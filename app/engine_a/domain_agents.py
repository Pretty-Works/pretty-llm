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
from app.tools.meeting_tool import meeting_list
from app.tools.navigate import navigate
from app.tools.project_tool import project_members, project_search
from app.tools.registry import RunContext, is_write
from app.tools.schedule_tool import schedule_create, schedule_list, schedule_update
from app.tools.task_tool import task_create, task_due_within, task_list, task_toggle_status, task_update
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
- "오늘 할일 뭐 있어?" / "오늘 몇 개 완료했어?" / "내일까지 마감인거 확인해줘" /
  "이번 주까지 마감인거 뭐 있어?" 처럼 특정 날짜(오늘 포함)까지의 마감 기준이면
  task_list 가 아니라 task_due_within 을 써라 — 상대 표현은 user_me 로 오늘을
  먼저 확인해 절대 날짜로 바꾼 뒤 untilDate 에 넣는다("내일"→오늘+1일). 완료/
  미완료 건수는 이 도구가 코드로 정확히 센 값이니 그대로 인용하라 — task_list
  표를 보고 눈으로 다시 세면 틀리기 쉽다(이월 항목 중복 세기 등). 특정 주(週)
  전체 조회(예: "다음 주 할일 목록 보여줘")만 task_list 를 써라.
- 할일 등록은 여러 건이어도 task_create 한 번(배치)으로 — 나눠 부르면 승인 카드가
  여러 장 뜬다. 회의록 후속조치를 할일로 만들 때도 한 번에 담아라.
- 할일은 본인 것만 만들고 바꿀 수 있다. 남의 할일 요청은 "본인 할일만 가능해요"라고
  거절하라.
- 프로젝트 할일의 마감일은 프로젝트 기간 안이어야 한다 — project_search 로 기간을
  먼저 확인하라. 개인 할일(projectId=null)은 제약이 없다.
- 완료 처리는 task_list 로 대상을 특정한 뒤 task_toggle_status(목표 상태 명시).
- task_list 결과에 마감(dueDate)이 이미 지났는데 미완료(□)인 할일이 있으면, 조용히
  목록만 보여주지 말고 먼저 짚어줘라 — "'OOO' 할일 마감이 지났어요, 일정이 지연된
  것 같아요"처럼 알리고 ask_user 로 다음 행동을 물어라(예: "완료 처리할까요?" /
  "마감일을 미룰까요?" / "그대로 둘까요?").
- 마감일 변경은 task_update 로 실제 처리한다. ★ task_update 는 PUT 전체 교체다 —
  content·projectId·dueDate 를 항상 셋 다 보내야 한다. 마감일만 바꾸는 요청이어도
  먼저 task_list 로 그 할일의 현재 content 와 projectId 를 확인해, 바뀌지 않는
  두 필드는 기존 값 그대로 채우고 dueDate 만 새 값으로 넣어 호출하라 — projectId 를
  비우면 프로젝트 할일이 개인 할일로 바뀌어 버리니 절대 임의로 null 을 넣지 마라.
  본인이 만든 할일만 수정할 수 있다.
- ★ "이번주 주간보고 작성해줘" 처럼 주간 업무 보고를 요청받으면, 이건 할일 목록
  하나만 보여주는 조회가 아니다 — 실제 업무 보고서처럼 여러 문단으로 써라(위의
  공통 규칙 "한두 문장으로만 보고하라"는 이 요청엔 적용하지 않는다). 답변 맨 앞에
  "작업 완료 보고" 같은 고정 문구를 붙이지 말고, 곧장 보고서 내용으로 시작하라.
  다음 순서로 자료를 모아 하나의 보고서로 종합하라:
    1) user_me 로 오늘/이번 주 범위를 확인한다.
    2) task_list(weekOffset=0) 로 이번 주 할일을 모아 완료/진행중을 정리한다.
    3) project_search(keyword="") 로 참여 중인 프로젝트를 확인하고, 각 프로젝트마다
       meeting_list 로 이번 주에 작성된 회의록이 있는지 살펴 "어떤 회의를 진행했는지"
       (제목·날짜·목적 요약)를 문장으로 풀어 써라 — meeting_detail 은 내용이 더 필요할
       때만 추가로 부른다.
    4) schedule_list(이번 주 월~일) 로 이번 주 일정 중 완료된 회의(MEETING) 외의
       주요 일정(외근·예정 회의 등)도 확인해 보고서에 반영한다.
  보고서는 "이번 주 업무 요약" → "완료한 업무" → "참석/진행한 회의" → "주요 일정"
  순서의 문단으로 구성하고, 각 항목의 수치(완료 건수 등)는 도구가 계산해 준 값을
  그대로 인용하라 — 직접 세지 마라. 조회했는데 실제로 아무 자료가 없으면(할일도
  회의도 일정도 0건) 지어내지 말고 "이번 주 기록된 업무가 없습니다"처럼 사실대로
  적어라."""

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
    return await _get("task", [user_me, project_search, task_list, task_due_within, task_create,
                               task_toggle_status, task_update, schedule_list, meeting_list,
                               analyze_impact, recall, doc_search, ask_user, navigate], TASK_PROMPT,
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
