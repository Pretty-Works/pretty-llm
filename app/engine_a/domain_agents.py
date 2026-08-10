"""
도메인 에이전트 공장 + 신규 도메인 4종 (할일 · 일정 · 지출 · 메일)

"승인 에이전트 패밀리" 원칙(8/4 합의): 뼈대(create_agent + HITL 미들웨어 +
RunContext)는 전부 같고, 메뉴판(도구)과 도메인 프롬프트만 다르다.
그 뼈대를 build_domain_agent() 하나로 묶었다 — 쓰기 도구는 registry 의
is_write() 로 자동 판별해 승인 게이트를 건다. 도구를 추가하면 게이트 설정을
따로 만질 필요가 없다 (빠뜨리는 사고 원천 차단). gmail_send_email 도 registry의
MCP_WRITE_TOOLS 덕에 is_write() 가 True 를 돌려주므로 여기서 별도 처리가 없다.

★ 메일(mail) 도메인만 다른 점 — 도구를 async 로 가져와야 한다.
  다른 도메인 도구는 전부 이 프로세스 안의 평범한 함수라 import 시점에 바로 쓸 수
  있지만, gmail 도구는 gmail-mcp 서버에 네트워크로 물어봐야 나온다
  (app/clients/gmail_mcp_client.py 의 get_gmail_tools(), MCP 서버가 꺼져 있으면
  빈 목록으로 폴백). 그래서 get_mail_agent() 는 다른 get_*_agent() 들처럼 정적
  목록을 넘기는 _get() 을 못 쓰고 직접 풀어서 짠다 — 아래 참고.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from app.clients.gmail_mcp_client import get_gmail_tools
from app.common.checkpoint import get_checkpointer
from app.config import settings
from app.engine_a.prompt_rules import COMMON_RULES
from app.tools.analyze import analyze_impact
from app.tools.memory_tool import doc_search, recall
from app.tools.ask_user import ask_user
from app.tools.expense_tool import budget_summary, expense_create, expense_list
from app.tools.navigate import navigate
from app.tools.project_tool import project_members, project_search
from app.tools.registry import RunContext, is_write
from app.tools.schedule_tool import schedule_create, schedule_list, schedule_update
from app.tools.task_tool import task_create, task_list, task_toggle_status
from app.tools.user_tool import user_me, user_search


def build_domain_agent(tools: list, domain_prompt: str, checkpointer,
                       description_prefix: str | None = None):
    """승인 에이전트 뼈대. 쓰기 도구(registry 기준)에 자동으로 승인 게이트를 건다.

    description_prefix 는 승인 카드 summary 로 흘러가는 한 줄 (60자 제한은 hitl 이 자름).
    """
    from langchain.agents.middleware import HumanInTheLoopMiddleware

    interrupt_on = {
        t.name: ({"allowed_decisions": ["approve", "reject"]} if is_write(t.name) else False)
        for t in tools
    }
    kwargs = {"interrupt_on": interrupt_on}
    if description_prefix:
        kwargs["description_prefix"] = description_prefix

    # 프롬프트 조립 순서가 중요하다: gpt-4o-mini 는 앞부분을 무겁게 보므로
    # "정체성 한 줄 → 절대 규칙(ask_user·navigate 강제) → 도메인 규칙" 순으로 놓는다.
    # 절대 규칙을 뒤에 붙였더니 텍스트로 되묻는 회귀가 실제로 났다 (2026-08-05).
    identity, _, rest = domain_prompt.partition("\n\n")
    system_prompt = identity + "\n" + COMMON_RULES + ("\n\n" + rest if rest else "")

    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
    return create_agent(
        model,
        tools=tools,
        system_prompt=system_prompt,
        context_schema=RunContext,
        middleware=[HumanInTheLoopMiddleware(**kwargs)],
        checkpointer=checkpointer,
    )


# ── 할일 에이전트 ──────────────────────────────────────────
TASK_PROMPT = """당신은 그룹웨어의 할일 담당 에이전트입니다.

도메인 규칙:
- 할일 등록은 여러 건이어도 task_create 한 번(배치)으로 — 나눠 부르면 승인 카드가
  여러 장 뜬다. 회의록 후속조치를 할일로 만들 때도 한 번에 담아라.
- 할일은 본인 것만 만들고 바꿀 수 있다. 남의 할일 요청은 "본인 할일만 가능해요"라고
  거절하라.
- 프로젝트 할일의 마감일은 프로젝트 기간 안이어야 한다 — project_search 로 기간을
  먼저 확인하라. 개인 할일(projectId=null)은 제약이 없다.
- 완료 처리는 task_list 로 대상을 특정한 뒤 task_toggle_status(목표 상태 명시)."""

# ── 일정 에이전트 ──────────────────────────────────────────
SCHEDULE_PROMPT = """당신은 그룹웨어의 일정 담당 에이전트입니다.

도메인 규칙:
- 일정을 추가·수정하기 전에 schedule_list 로 그 시간대를 먼저 확인하라. 서버는
  겹침을 막지 않는다 — 겹치면 사용자가 알 수 있게 답변과 미리보기에 명시하라.
- 참가자는 이름이 아니라 userId 다. 프로젝트 사람이면 project_members, 아니면
  user_search 로 변환하라 (본인은 참가자에서 제외).
- 휴가는 이 도메인이 아니다 — type=LEAVE 로 일정을 만들지 마라. 연차 요청이면
  연차 신청으로 처리해야 한다고 안내하라. 휴가 일정(isLeave)은 수정도 못 한다.
- 일정 수정 시 참가자 "추가"는 기존 명단에 새 사람을 합친 전체 목록으로 보내야
  한다 — 새 사람만 보내면 기존 참가자가 전부 빠진다."""

# ── 지출 에이전트 ──────────────────────────────────────────
EXPENSE_PROMPT = """당신은 그룹웨어의 지출·예산 담당 에이전트입니다.

도메인 규칙:
- 금액은 원 단위 정수다. "12만원"=120000. 해석이 조금이라도 애매하면 지어내지
  말고 ask_user 로 확인하라 — 금액 오류가 이 도메인의 최악 사고다.
- 지출 사용일은 프로젝트 기간 안이어야 한다 — project_search 로 기간 확인.
- 등록 전에 budget_summary 로 예산 상황을 파악하고, 이 지출로 집행률이 100%를
  넘으면 답변에 알려라.
- "제일 큰 지출" 질문은 expense_list 의 sort=AMOUNT_DESC 를 쓴다."""

# ── 메일 에이전트 ──────────────────────────────────────────
MAIL_PROMPT = """당신은 그룹웨어의 메일(Gmail) 담당 에이전트입니다.

도메인 규칙:
- gmail_connection_status 로 미연결 상태를 확인했다면 "Gmail 연동을 먼저 해주세요"
  라고 안내하고 다른 gmail 도구는 더 부르지 마라 — 연동 절차는 이 에이전트가 대신
  할 수 없다(별도 OAuth 화면에서 해야 한다).
- 메일 발송 전 받는 사람(to)·제목(subject)·본문(body) 이 명확한지 확인하라. 받는
  사람을 이름으로만 말하면 이메일 주소를 지어내지 말고 ask_user 로 확인하라 —
  잘못된 주소로 나간 메일은 되돌릴 수 없다.
- 검색 결과가 많으면 발신자·제목·날짜 위주로 목록만 요약해 보여주고, 본문 전체가
  필요할 때만 gmail_get_email 로 상세를 가져와라.
- "우선순위 분석해서 메일로 보내줘"처럼 프로젝트 심층 분석이 먼저 필요한 요청이면
  분석 내용을 지어내지 말고 analyze_impact 로 실제 분석 결과를 받아온 뒤, 그 결과를
  메일 본문으로 정리해 보내라."""


_agents: dict = {}


async def _get(key: str, tools: list, prompt: str, prefix: str):
    if key not in _agents:
        _agents[key] = build_domain_agent(tools, prompt, await get_checkpointer(),
                                          description_prefix=prefix)
    return _agents[key]


async def get_task_agent():
    return await _get("task", [user_me, project_search, task_list, task_create,
                               task_toggle_status, analyze_impact, recall, doc_search, ask_user, navigate], TASK_PROMPT,
                      "할일 등록/변경 요청입니다.")


async def get_schedule_agent():
    return await _get("schedule", [user_me, user_search, project_members, schedule_list,
                                   schedule_create, schedule_update, analyze_impact, recall, doc_search, ask_user, navigate],
                      SCHEDULE_PROMPT, "일정 등록/변경 요청입니다.")


async def get_expense_agent():
    return await _get("expense", [user_me, project_search, budget_summary, expense_list,
                                  expense_create, analyze_impact, recall, doc_search, ask_user, navigate], EXPENSE_PROMPT,
                      "지출 등록 요청입니다.")


async def get_mail_agent():
    """gmail 도구가 async 로만 나오므로 _get() 을 그대로 못 쓴다 — 직접 캐시를 관리한다.

    MCP 서버가 죽어 있어 gmail_* 도구가 하나도 안 붙은(빈 목록) 상태로 만들어졌다면
    캐시하지 않는다 — 그대로 캐시하면 서버가 살아난 뒤에도 재요청 없이는 계속 빈
    도구로 남는다. gmail 도구가 하나라도 붙었을 때만 다른 도메인처럼 재사용한다.
    """
    if "mail" in _agents:
        return _agents["mail"]

    tools = [user_me, ask_user, analyze_impact, *await get_gmail_tools()]
    agent = build_domain_agent(tools, MAIL_PROMPT, await get_checkpointer(),
                               description_prefix="메일 조회/발송 요청입니다.")
    if any(getattr(t, "name", "").startswith("gmail_") for t in tools):
        _agents["mail"] = agent
    return agent
