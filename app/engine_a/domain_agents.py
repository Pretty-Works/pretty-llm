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
from app.tools.read_errors import read_error_middleware
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

    # temperature 를 안 넘기면 provider 기본값으로 돌아 같은 질문에도 도구 선택이
    # 흔들린다 (실측: 동일 입력 5회에 응답 2가지). 라우팅·도구 선택은 결정적이어야 한다.
    #
    # ★ 2026-08-13 추가 — max_retries·timeout 이 지금까지 여기(engine_a)엔 안 넘어가고
    #   있었다(provider SDK 기본값 그대로 — 재시도가 거의 없거나 짧다). engine_b 의
    #   get_llm()(app/common/llm_client.py)은 처음부터 이 두 값을 settings 에서
    #   받아 왔는데, engine_a 의 도메인 에이전트(할일·일정·지출·메일·재계획·회의·
    #   휴가·연차 전부 이 build_domain_agent 를 거친다)만 빠져 있었다 — 동시 사용자가
    #   늘며 보고된 429(RateLimitError) traceback이 전부 "agent.astream() → LangGraph
    #   → OpenAI" 경로였던 이유가 이거였다. max_retries>0 이면 openai SDK 가 429/5xx를
    #   지수 백오프(+지터)로 자동 재시도한다 — Run 을 처음부터 다시 돌리는 게
    #   아니라 실패한 그 HTTP 호출 한 번만 재시도되므로, 이미 실행된 Tool·HITL
    #   처리가 중복되지 않는다.
    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider,
                                temperature=settings.llm_temperature,
                                max_retries=settings.llm_max_retries,
                                timeout=settings.llm_timeout)
    return create_agent(
        model,
        tools=tools,
        system_prompt=system_prompt,
        context_schema=RunContext,
        middleware=[HumanInTheLoopMiddleware(**kwargs), read_error_middleware()],
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
  한다 — 새 사람만 보내면 기존 참가자가 전부 빠진다.
- ★ 옮길 일정과 옮길 시점이 대화에 이미 있으면 **되묻지 말고 schedule_update 로
  곧장 실행하라.** "며칠로 옮길까요?" 처럼 되묻는 건 사용자가 이미 답한 것을 또
  묻는 것이다 — 날짜가 "복귀 이후"처럼 범위로만 주어졌으면 그 범위의 첫 근무일을
  네가 정해서 진행하고, 정한 날짜를 답변에 밝혀라.
- ★ 휴가로 등록된 일정(isLeave)은 수정할 수 없다. 옮겨 달라는 요청을 받으면
  휴가 일정 말고 **그 기간에 걸친 회의·미팅 일정**을 대상으로 삼아라."""

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
- 메일 관련 요청을 받으면 다른 gmail 도구보다 먼저 gmail_connection_status 로
  연결 여부를 확인하라. 미연결이면 이렇게 처리한다:
    1) gmail_connect_url 을 호출해 실제 Google 로그인 URL을 받는다.
    2) navigate(targetScreen="GMAIL_CONNECT", label="Gmail 연동하러 가기",
       params={"authorizeUrl": 1)에서 받은 URL}) 을 호출한다.
    3) ★ 2026-08-13 임시 조치 — 프론트가 아직 이 action(navigate params의
       authorizeUrl)을 버튼으로 그려주는 걸 구현 안 해서, navigate 만 호출하면
       사용자에게 아무 것도 안 보인다. 프론트가 반영하기 전까지는 **텍스트
       답변에도 그 URL을 그대로 적어라**(예: "Gmail 연동이 필요해요. 아래
       링크를 눌러 로그인해주세요: {URL}") — 이 항목은 프론트가 버튼을 만들면
       지워도 된다(그 전까진 navigate 호출도 그대로 유지 — 버튼 켜지는 순간
       자동으로 이어받게).
  gmail_connect_url 이 {"error": ...} 를 돌려주면(run 조회 실패 등) URL 없이
  "잠시 후 다시 시도해달라"고 안내하고 navigate 는 호출하지 마라. navigate 호출
  후 다른 gmail 도구는 더 부르지 마라. 연결돼 있으면 이 확인 결과만으로 조용히 다음 단계(검색 등)로
  진행하고, 연결 상태 자체를 사용자에게 보고하지 마라.
- ★ 자연어 요청을 있는 그대로 gmail_search_emails 의 query 에 옮겨 담아라 —
  대화에 실제로 언급된 조건만 Gmail 검색 문법으로 변환하고, 언급 안 된 조건을
  지어내서 채우지 마라. 여러 조건은 공백으로 이어 붙이면 AND 로 합쳐진다.
    - 발신자 언급("OOO한테 온 메일") → from:OOO (대화에 나온 이름/이메일 그대로)
    - 제목 키워드("제목에 회의 들어간 메일") → subject:회의
    - 읽음 상태("안읽은 메일"/"읽은 메일") → is:unread / is:read
    - 첨부파일("첨부파일 있는 메일") → has:attachment
    - 기간("지난주 메일"·"이번 달 메일" 등 상대 날짜) → after:YYYY/MM/DD
      before:YYYY/MM/DD (Gmail 날짜 문법은 슬래시 구분). 상대 날짜는 절대 규칙
      대로 user_me 로 오늘을 먼저 확인해 절대 날짜로 계산한 뒤 채워라 — 추측 금지.
    - 예: "지난주에 김민수가 보낸 안읽은 메일" → from:김민수 is:unread
      after:2026/08/03 before:2026/08/10
  ★ "가장 최근 메일이 뭐야?" / "최근 메일 보여줘"처럼 위 조건 중 아무것도 말하지
  않은 요청은 **발신자를 되묻지 말고** query="" 로 곧장 조회하라 — 조건을 안 밝힌
  건 모호한 게 아니라 '조건 없음'이라는 뜻이다. 결과는 이미 최신순으로 정렬돼서
  온다(messages[0]이 가장 최근) — 필요한 만큼(예: "가장 최근 메일"이면 1건)을
  그대로 답하면 된다. 검색 결과에 여러 발신자가 섞여 있는 것 자체는 되물을 이유가
  아니다 — 공통 규칙의 "동명이인이 나오면 ask_user로 확인하라"는 사람 검색
  (참가자·담당자 지정처럼 특정 한 명을 골라야 하는 경우)에서 같은 이름이 여럿일
  때 얘기지, 메일 검색 결과에 여러 명의 발신자가 섞여 있는 것과는 다른 상황이다.
  진짜로 조건 자체가 모호한 경우(예: 대화에 나온 이름과 겹치는 사람이 여럿이라
  누굴 말하는지 실제로 특정이 안 될 때)만 ask_user 로 확인하라.
- 메일 발송 전 받는 사람(to)·제목(subject)·본문(body) 이 명확한지 확인하라. 받는
  사람을 이름으로만 말하면 이메일 주소를 지어내지 말고 ask_user 로 확인하라 —
  잘못된 주소로 나간 메일은 되돌릴 수 없다.
- 검색 결과가 여러 건이면 발신자·제목·날짜 위주로 목록만 요약해 보여주고, 본문
  전체가 필요할 때만 gmail_get_email 로 상세를 가져와라. 단, "가장 최근 메일"처럼
  단건을 지정한 요청이면 목록이 아니라 그 한 건만 답하라.
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
                      "할 일 등록/변경")


async def get_schedule_agent():
    return await _get("schedule", [user_me, user_search, project_members, schedule_list,
                                   schedule_create, schedule_update, analyze_impact, recall, doc_search, ask_user, navigate],
                      SCHEDULE_PROMPT, "일정 등록/변경")


async def get_expense_agent():
    return await _get("expense", [user_me, project_search, budget_summary, expense_list,
                                  expense_create, analyze_impact, recall, doc_search, ask_user, navigate], EXPENSE_PROMPT,
                      "지출 등록")


async def get_mail_agent():
    """gmail 도구가 async 로만 나오므로 _get() 을 그대로 못 쓴다 — 직접 캐시를 관리한다.

    MCP 서버가 죽어 있어 gmail_* 도구가 하나도 안 붙은(빈 목록) 상태로 만들어졌다면
    캐시하지 않는다 — 그대로 캐시하면 서버가 살아난 뒤에도 재요청 없이는 계속 빈
    도구로 남는다. gmail 도구가 하나라도 붙었을 때만 다른 도메인처럼 재사용한다.
    """
    if "mail" in _agents:
        return _agents["mail"]

    tools = [user_me, ask_user, analyze_impact, navigate, *await get_gmail_tools()]
    agent = build_domain_agent(tools, MAIL_PROMPT, await get_checkpointer(),
                               description_prefix="메일 조회/발송")
    if any(getattr(t, "name", "").startswith("gmail_") for t in tools):
        _agents["mail"] = agent
    return agent
