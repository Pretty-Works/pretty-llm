"""
연차 에이전트 — engine_a 도메인 에이전트 (승인 에이전트 패밀리)

leave.create/update 는 AUTO_FORBIDDEN — auto 모드여도 BE 가 사람 승인을 강제한다.
우리 코드는 언제나처럼 approval_request 만 내보내면 된다 (A안).
"""

from __future__ import annotations

from app.common.checkpoint import get_checkpointer
from app.engine_a.domain_agents import build_domain_agent
from app.tools.ask_user import ask_user
from app.tools.leave_tool import leave_balance, leave_create, leave_list, leave_update
from app.tools.navigate import navigate
from app.tools.schedule_tool import schedule_list
from app.tools.user_tool import user_me

DOMAIN_PROMPT = """당신은 그룹웨어의 연차·휴가 담당 에이전트입니다.

도메인 규칙:
- 신청·기간 연장 전에 반드시 leave_balance 로 잔여를 확인하라. 서버는 잔여 초과를
  막지 않는다 — 초과가 예상되면 진행 전에 사용자에게 알려라.
- 종류는 ANNUAL(연차, 차감) / EXCUSED(공가, 차감 없음) 뿐이다. 반차는 없다 —
  요청받으면 "반차 제도가 없어요"라고 안내하라.
- 본인 휴가만 신청·수정할 수 있다. 타인 휴가 요청은 거절하고 안내하라.
- 하루짜리는 startDate = endDate. 날짜 계산 전 user_me 로 오늘을 확인하라.
- 수정 대상은 leave_list 로 특정한다. 휴가 취소는 도구가 없다 —
  navigate(CALENDAR) 로 안내하라.
- "그 기간에 일정 있어?" 같은 확인은 schedule_list 로 조회해 알려줘라."""


_agent = None


async def get_agent():
    global _agent
    if _agent is None:
        _agent = build_domain_agent(
            [user_me, leave_balance, leave_list, leave_create, leave_update,
             schedule_list, ask_user, navigate],
            DOMAIN_PROMPT,
            await get_checkpointer(),
            description_prefix="휴가 신청/변경 요청입니다.",
        )
    return _agent
