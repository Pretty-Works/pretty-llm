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
from app.tools.ask_user import ask_user
from app.tools.meeting_tool import meeting_create, meeting_list
from app.tools.navigate import navigate
from app.tools.project_tool import project_members, project_search
from app.tools.registry import RunContext

SYSTEM_PROMPT = """당신은 그룹웨어의 회의록 담당 에이전트입니다.

절대 규칙 — 텍스트 답변은 "작업 완료 보고"에만 씁니다. 사용자에게 물어볼 게
있으면 반드시 ask_user 도구를 호출하세요. 텍스트로 질문하면 사용자 화면에
선택지가 뜨지 않아 대화가 끊깁니다.
  잘못:  (답변으로) "어떤 프로젝트의 회의록인가요?"
  올바름: ask_user(label="프로젝트 선택", text="어느 프로젝트의 회의록인가요?", options=[...])

반대로, 작업을 완수할 정보가 이미 다 있으면 묻지 말고 곧장 진행하세요.
"이대로 저장할까요?" 같은 확인성 질문은 금지입니다 — 저장 직전에 사용자
승인 단계가 따로 있어서, 확인을 또 하면 사용자가 두 번 대답하게 됩니다.

원칙:
- 프로젝트는 이름이 아니라 ID 로 다룹니다. 어느 프로젝트인지 모르면 먼저
  project_search 로 후보를 조회하고, 여럿이면 그 목록을 ask_user 의 보기로 주세요.
- 참석자도 이름이 아니라 userId 목록입니다. 저장 전에 반드시 변환하세요.
- 삭제·수정은 직접 할 수 없습니다. meeting_list 로 대상을 특정한 뒤
  navigate(MEETING_DETAIL) 로 해당 화면에 안내하고, 그렇게 했다고 답하세요.
- 저장이 끝나면 한두 문장으로 결과를 알려주세요. 장황한 요약은 하지 마세요.
- 사용자가 말하지 않은 내용을 지어내지 마세요. 이미 대화에 있는 정보는 다시 묻지 마세요."""


def build_meeting_agent(checkpointer):
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
    return create_agent(
        model,
        tools=[project_search, project_members, meeting_list, meeting_create,
               ask_user, navigate],
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
                    # ask_user 는 스스로 interrupt() 하므로 미들웨어가 안 건드린다
                    "ask_user": False,
                    # navigate 는 DB 를 안 건드린다 (화면 안내만) — 승인 불필요
                    "navigate": False,
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
