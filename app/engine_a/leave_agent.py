"""
연차 에이전트 — engine_a 의 두 번째 도메인 에이전트 (승인 에이전트 패밀리)

meeting_agent 와 뼈대가 같다: create_agent + HITL 미들웨어 + ask_user + RunContext.
다른 건 메뉴판(연차 도구)과 프롬프트뿐 — "도메인별 에이전트 = 같은 승인
에이전트를 메뉴만 바꿔 찍어낸다"는 합의(8/4)의 구현이다.

겸장 도구: 도메인 걸침 요청("연차 기간에 회의 있어?")을 위해 인접 도메인의
조회 도구를 몇 개 같이 물린다. 쓰기는 자기 도메인 것만.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from app.common.checkpoint import get_checkpointer
from app.config import settings
from app.tools.ask_user import ask_user
from app.tools.leave_tool import leave_balance, leave_create
from app.tools.meeting_tool import meeting_list
from app.tools.project_tool import project_search
from app.tools.registry import RunContext

SYSTEM_PROMPT = """당신은 그룹웨어의 연차 담당 에이전트입니다.

절대 규칙 — 텍스트 답변은 "작업 완료 보고"에만 씁니다. 사용자에게 물어볼 게
있으면 반드시 ask_user 도구를 호출하세요.
반대로 작업을 완수할 정보가 이미 다 있으면 묻지 말고 곧장 진행하세요.
"이대로 신청할까요?" 같은 확인성 질문은 금지입니다 — 신청 직전에 사용자
승인 단계가 따로 있습니다.

원칙:
- 날짜가 상대 표현("다음주 화요일")이면 정확한 날짜(YYYY-MM-DD)를 계산하고,
  계산이 애매하면 ask_user 로 확인하세요.
- 신청 전에 leave_balance 로 잔여 일수가 충분한지 확인하세요.
- 하루 연차는 startDate = endDate 입니다. 반차는 leaveType 으로 구분합니다.
- 신청이 끝나면 한두 문장으로 결과를 알려주세요.
- 사용자가 말하지 않은 내용을 지어내지 마세요."""


def build_leave_agent(checkpointer):
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
    return create_agent(
        model,
        tools=[leave_balance, leave_create, ask_user,
               project_search, meeting_list],          # 뒤 2개는 겸장(조회 전용)
        system_prompt=SYSTEM_PROMPT,
        context_schema=RunContext,
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={
                    "leave_create": {"allowed_decisions": ["approve", "reject"]},
                    "leave_balance": False,
                    "project_search": False,
                    "meeting_list": False,
                    "ask_user": False,     # 스스로 interrupt 하므로 미들웨어 제외
                },
                description_prefix="연차 신청 요청입니다.",
            )
        ],
        checkpointer=checkpointer,
    )


_agent = None


async def get_agent():
    global _agent
    if _agent is None:
        _agent = build_leave_agent(await get_checkpointer())
    return _agent
