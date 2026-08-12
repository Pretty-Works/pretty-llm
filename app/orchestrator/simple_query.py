"""
simple_query 핸들러 — 조회 전용 에이전트

engine_a 와 같은 create_agent 지만 세 가지가 없다:
  · 쓰기 도구 없음        → 승인 게이트 자체가 불필요 (미들웨어 없음)
  · ask_user 없음         → interrupt 가 절대 없다 = 한 세그먼트에 반드시 done
  · checkpointer 없음     → 재개할 일이 없으니 상태를 남기지 않는다

"조회는 게이트 없이 반환"(팀 다이어그램)이 이 구성으로 구현된다.
정보가 애매하면 done.answer 로 물어보면 된다 — 다음 발화는 새 Run 으로 오고,
직전 대화가 messages(최근 10건)에 실려 오므로 맥락이 이어진다.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from app.config import settings
from app.tools.expense_tool import budget_summary, expense_list
from app.tools.leave_tool import leave_balance, leave_list
from app.tools.meeting_tool import meeting_detail, meeting_list
from app.tools.memory_tool import doc_search, recall
from app.tools.milestone_tool import milestone_list
from app.tools.project_tool import project_members, project_search
from app.tools.read_errors import read_error_middleware
from app.tools.registry import RunContext
from app.tools.schedule_tool import schedule_list
from app.tools.task_tool import task_list
from app.tools.user_tool import user_me, user_search

SYSTEM_PROMPT = """당신은 그룹웨어의 조회 담당 에이전트입니다. 묻는 것에만 간결히 답합니다.

원칙:
- "어제"·"이번 주" 같은 상대 날짜가 나오면 user_me 를 먼저 불러 오늘을 확인하세요.
- 프로젝트를 이름으로 말하면 project_search 로 ID 를 찾은 뒤 조회하세요.
- 조회 결과에 없는 내용을 지어내지 마세요. 없으면 없다고 답하세요.
- 남의 휴가 사유 같은 가려진 값(null)은 답변에 싣지 마세요.
- ★ 조회 결과의 **[123] 같은 대괄호 번호와 영어 도구 이름은 내부용**입니다. 답변에
  그대로 쓰지 마세요 — 사용자는 그게 뭔지 모릅니다. 이름으로만 말하세요.
    잘못: "[58] API 명세 정리"     올바름: "API 명세 정리"
- ★ 개수·비율은 **도구가 준 집계값을 그대로** 쓰세요. 목록을 보고 직접 세지 마세요.
- "예전에·지난번에·저번에" 같은 과거 참조가 나오면 recall 로 먼저 찾으세요.
- 규정·정책·문서 근거가 필요한 질문("규정상 ~돼?")은 doc_search 로 찾아 답하세요.
- 조회 결과 0건은 에러가 아닙니다 — "없음"으로 답하고, 대상을 못 찾은 것이면 되물으세요.
- 결과에 잘림(truncated) 표시가 있으면 검색어나 기간을 좁혀 다시 조회하세요.
- 동명이인이 나오면 임의로 고르지 마세요 — 부서·직책을 나열해 누구인지 되물으세요.
- 답은 2~3문장 이내. 목록은 항목당 한 줄로.
- 무엇을 조회할지 애매하면 짧게 되물어보세요 (이 답변 자체가 질문이어도 됩니다)."""

# 조회 13종 전부 — 쓰기 도구가 하나도 없으므로 승인 게이트 자체가 불필요
READ_TOOLS = [user_me, user_search, project_search, project_members, milestone_list,
              task_list, meeting_list, meeting_detail, budget_summary, expense_list,
              schedule_list, leave_balance, leave_list, recall, doc_search]


def build_simple_agent():
    # temperature 미전달 시 provider 기본값으로 돈다 — domain_agents 와 같은 이유로 고정
    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider,
                            temperature=settings.llm_temperature)
    return create_agent(
        model,
        tools=READ_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        context_schema=RunContext,
        # 승인 미들웨어·checkpointer 없음 — 위 모듈 주석 참고.
        # read_error_middleware 는 승인 게이트가 아니라 조회 실패 복구용이라 예외.
        middleware=[read_error_middleware()],
    )


_agent = None


async def get_agent():
    """다른 핸들러와 시그니처를 맞추기 위한 async (내부는 동기 생성)."""
    global _agent
    if _agent is None:
        _agent = build_simple_agent()
    return _agent
