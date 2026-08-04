"""
회의록 에이전트 — Engine A 의 첫 v2 도메인 에이전트

vacation_agent 와 같은 패턴(create_agent + HITL 미들웨어)이되, v2 계약에 맞춘 점:
  - context_schema 가 AuthContext(user_id) → RunContext(run_id) 로 바뀜.
    신원은 runId 가 대신한다 (내부 API 가 X-Run-Id 로 요청자를 역산).
  - 도구가 실제 내부 API(mock)를 부른다. 쓰기(meeting_create)는 승인 토큰 필요.
  - 체크포인터가 Async 판. 스트림을 닫고 /resume 으로 이어붙이는 전제.

도구 선택 순서(조회→변환→저장)는 코드로 강제하지 않는다 — 도구 docstring 의
안내를 읽고 LLM 이 스스로 짠다. 그래서 이 파일에는 흐름 제어 코드가 없다.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from app.common.checkpoint import get_checkpointer
from app.config import settings
from app.tools.meeting_tool import meeting_create, meeting_list
from app.tools.project_tool import project_members, project_search
from app.tools.registry import RunContext

SYSTEM_PROMPT = """당신은 그룹웨어의 회의록 담당 에이전트입니다.

원칙:
- 프로젝트는 이름이 아니라 ID 로 다룹니다. 사용자가 이름으로 말하면 먼저 검색해 ID 를 찾으세요.
- 참석자도 이름이 아니라 userId 목록입니다. 저장 전에 반드시 변환하세요.
- 저장이 끝나면 한두 문장으로 결과를 알려주세요. 장황한 요약은 하지 마세요.
- 사용자가 말하지 않은 내용을 지어내지 마세요. 회의 내용이 없으면 없는 대로 저장하세요."""


def build_meeting_agent(checkpointer):
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
    return create_agent(
        model,
        tools=[project_search, project_members, meeting_list, meeting_create],
        system_prompt=SYSTEM_PROMPT,
        context_schema=RunContext,
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={
                    # 쓰기만 멈춘다. 조회 승인은 규격에서 폐지됨(8/3).
                    "meeting_create": {"allowed_decisions": ["approve", "reject"]},
                    "project_search": False,
                    "project_members": False,
                    "meeting_list": False,
                },
                description_prefix="회의록 저장 요청입니다.",
            )
        ],
        checkpointer=checkpointer,
    )


_agent = None


async def get_agent():
    """싱글톤 + 지연 생성. 체크포인터가 async 라 팩토리도 async 다."""
    global _agent
    if _agent is None:
        _agent = build_meeting_agent(await get_checkpointer())
    return _agent
