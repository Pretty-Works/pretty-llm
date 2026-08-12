"""
연차 에이전트 — engine_a 도메인 에이전트 (승인 에이전트 패밀리)

leave.create/update 는 AUTO_FORBIDDEN — auto 모드여도 BE 가 사람 승인을 강제한다.
우리 코드는 언제나처럼 approval_request 만 내보내면 된다 (A안).

★ 엔진 A → 엔진 B 분기 (8/12 설계 반영):
  leave_create 를 곧장 부르지 않고, 이 에이전트가 먼저 스스로 1차 판단
  (잔여 연차 · 겹치는 일정 · 마감 할일)을 한다. 문제가 없으면 바로 승인 요청,
  판단이 애매하거나 위험 신호가 있으면 analyze_impact(엔진 B 심층 분석)를
  거쳐 그 결과를 갖고 다시 판단한다. 구체적 절차는 DOMAIN_PROMPT 참고.
"""

from __future__ import annotations

from app.common.checkpoint import get_checkpointer
from app.engine_a.domain_agents import build_domain_agent
from app.tools.analyze import analyze_impact
from app.tools.ask_user import ask_user
from app.tools.leave_tool import leave_balance, leave_create, leave_list, leave_update
from app.tools.memory_tool import doc_search, recall
from app.tools.navigate import navigate
from app.tools.schedule_tool import schedule_list
from app.tools.task_tool import task_list
from app.tools.user_tool import user_me

DOMAIN_PROMPT = """당신은 그룹웨어의 연차·휴가 담당 에이전트입니다.

★ 연차 신청(leave_create) 전 1차 판단 절차 — leave_create 를 곧장 부르지 말고
   반드시 아래 순서로 먼저 확인한다:
  1. 날짜가 상대 표현("내일"·"다음 주" 등)이면 user_me 로 오늘을 먼저 확인해
     절대 날짜로 바꾼다.
  2. leave_balance 로 잔여 연차를 확인한다.
  3. schedule_list(신청 기간)로 그 기간에 겹치는 일정이 있는지 확인한다.
  4. task_list(신청 기간이 속한 주의 weekOffset, projectId=null)로 그 기간에
     마감(dueDate)인 본인 할일이 있는지 확인한다.
  5. 판단 분기:
     - 잔여 부족 없음 + 겹치는 일정 없음 + 마감 할일 없음(또는 미뤄도 무방해
       보임) → 추가 분석 없이 바로 leave_create 를 불러 승인을 요청한다.
     - 잔여가 빠듯하거나, 그 기간에 마감인 할일·일정이 있어 "미뤄도 되는지"를
       이 에이전트 혼자 판단하기 어렵거나, 사용자가 영향 판단을 원하면 →
       leave_create 전에 analyze_impact 로 심층 분석(엔진 B)을 받는다. 겹치는
       일정·할일 내용을 질문에 그대로 담아라
       (예: "OOO가 2026-08-13 연차를 쓰면 'Kafka 구축' 마감(2026-08-14)에
       위험이 있는가?"). 분석 결과를 사용자에게 보여준 뒤 그래도 진행을
       원하면 그 때 leave_create 로 승인을 요청한다.
     - 그래도 애매하면 지어내지 말고 ask_user 로 확인하라.

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
             schedule_list, task_list, analyze_impact, recall, doc_search,
             ask_user, navigate],
            DOMAIN_PROMPT,
            await get_checkpointer(),
            description_prefix="휴가 신청/변경",
        )
    return _agent
