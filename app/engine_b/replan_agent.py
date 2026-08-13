# app/engine_b/replan_agent.py
"""
Replan 에이전트 — 엔진A 와 똑같은 승인 에이전트 뼈대(engine_a.domain_agents.
build_domain_agent)를 재사용해 "3안 제시 → 선택 → 저장 승인 → 반영 승인" HITL 을
처리한다.

바뀐 건 상호작용 방식뿐이다. 3안 생성 자체(analysis_router → scenario_executor →
tradeoff)는 예전 replan_service.py 가 쓰던 파이프라인 그대로다 — replan_tools.py 가
그 파이프라인을 build_domain_agent() 가 이해하는 @tool 세 개로 감쌌을 뿐이다.

  예전: entry.py(정규식 파싱) + suggestion_store.py(인메모리 dict, 다중 워커에 약함)
  지금: LangGraph interrupt/resume + checkpointer (엔진A 도메인 에이전트와 완전히 동일한
        인프라 — app/api/agent.py 의 /resume 엔드포인트, app/common/hitl.py 를 그대로 재사용)

★ 2026-08-09 BE 스펙 전면 개정 — 저장(replan_save)도 이제 승인 대상이다(예전엔
  AUTO_ALLOWED 라 propose_replan_scenarios 안에서 바로 write 했다). 그래서 흐름이
  "3안 제시 → 선택 → 반영 승인" 2단계에서 "3안 제시 → 선택 → 저장 승인 → 반영 승인"
  3단계(승인 2회)로 늘었다.

★ gmail 도구도 여기 그대로 얹었다 — "엔진A/B가 mcp_tools.py를 각자 따로 호출하는 게
  아니라 같은 걸 참조하면 안 되냐"는 질문의 답이 바로 이거다. gmail-mcp 서버(mcp_tools.py)
  쪽은 손댈 필요가 전혀 없었다 — engine_a/domain_agents.py의 get_mail_agent()가 이미
  하던 것과 똑같이 app.clients.gmail_mcp_client.get_gmail_tools()를 한 번 더 부르기만
  하면 된다. 이게 가능한 이유는 이 파일이 build_domain_agent()를 쓰기 때문이다 — 그
  뼈대가 이미 "쓰기 도구는 registry.is_write()로 자동 판별해 승인 게이트를 건다"를
  구현하고 있어서, gmail_send_email이 여기 섞여도 원래 mail 도메인에서와 똑같이
  자동으로 사람 승인이 걸린다(별도 코드 불필요). run_id 배선도 마찬가지로 공짜다 —
  이 에이전트도 결국 hitl.stream_run()/stream_command() → hitl._drive()를 거치므로,
  gmail 도구가 읽는 current_run_id contextvar는 _drive()가 이미 세팅해준다(mail
  도메인 때 고쳤던 그 한 줄이 여기도 그대로 적용된다).
"""
from __future__ import annotations

from app.clients.gmail_mcp_client import get_gmail_tools
from app.common.checkpoint import get_checkpointer
from app.engine_a.domain_agents import build_domain_agent
from app.engine_b.replan_tools import (
    propose_replan_scenarios,
    replan_apply,
    replan_save,
)
from app.tools.ask_user import ask_user
from app.tools.navigate import navigate

DOMAIN_PROMPT = """당신은 그룹웨어의 재계획(Replan) 담당 에이전트입니다.

도메인 규칙:
- 재계획 요청이 오면 propose_replan_scenarios(query=...) 를 호출해 조정안 3개를
  만들어라. query 에는 "어느 프로젝트가 왜 재계획이 필요한지"를 한두 문장으로
  정리해 넣어라. 대화 맨 앞의 화면 컨텍스트("현재 화면 / 입력된 폼 값")나 사용자
  발화에 프로젝트 ID가 숫자로 나와 있으면, query 텍스트에 적는 것과 별개로
  project_id 인자에도 그 값을 그대로 넣어라 — 이름만으로는 라우터가 프로젝트를
  못 찾을 수 있다.
- 3안이 나오면 곧장 저장하지 마라. 먼저 ask_user 로 3안을 보기로 제시하고 하나를
  고르게 하라. options 에는 한글 라벨(예: "일정 조정 (추천)")을 넣고, text 에는
  3안의 핵심 차이(일정회복·비용·리스크)를 요약해라.
- 사용자가 하나를 고르면, propose_replan_scenarios 결과에 나온 scenarioType·
  summary·risk·operations 를 그대로 옮겨 적어 replan_save(projectId, reason,
  scenarios) 를 호출해 저장하라. scenarios 인자엔 사용자가 고른 안 하나만 담아도
  되고, 나중에 마음이 바뀔 수 있으니 3안 전체를 담아도 된다. reason 에는 왜
  재계획이 필요한지 한 문장으로 적어라. 저장도 DB write 라 승인이 필요하다 —
  미들웨어가 자동으로 승인 카드를 띄운다.
- 저장이 끝나 replanId 를 돌려받으면, 그 replanId·projectId 와 사용자가 고른
  scenarioType(영문 코드: REALLOCATE/EXTEND/REDUCE_SCOPE)을 그대로 replan_apply
  인자로 옮겨 적어 반영하라 — 새로 짐작해 지어내지 마라. 이것도 승인이 필요하다.
- 사용자가 3안을 전부 거절하면(예: "다 별로야", "이거 말고 다른 방법 없어?"),
  곧바로 다시 만들지 말고 먼저 ask_user 로 "어떤 부분이 마음에 안 드시나요?
  원하시는 방향을 알려주세요" 라고 물어라. 답을 받으면 그 내용을 반영해 query 를
  다시 써서 propose_replan_scenarios 를 한 번 더 호출하라 — 이 재생성은 세션당
  딱 1회만 가능하다(비용 문제). propose_replan_scenarios 가 재생성 한도 초과
  문구를 돌려주면, 있는 3안 중에서 다시 골라달라고 안내하라.
- 사용자가 특정 안을 고르면서 동시에 수정을 요청하면(예: "2번인데 예산은 그대로
  둬줘"), 그 안을 그대로 저장하지 마라 — 그 수정 내용을 query 에 담아
  propose_replan_scenarios 를 다시 호출해 새 안을 받아 다시 확인시켜라. 이것도
  위와 같은 재생성 1회 한도를 공유한다.
- replan_save · replan_apply 는 둘 다 실제 DB write 다 — 사용자가 명확히
  동의했을 때만 호출하고, 인자는 항상 앞 단계 결과를 그대로 옮겨 적어라(요약·
  재작성 금지 — 승인 카드에 뜨는 내용이 그대로 실행된다).
- 반영(replan_apply)까지 끝난 뒤 사용자가 "팀원에게 메일로 알려줘" 처럼 메일 발송을
  요청하면, gmail_connection_status 로 먼저 연결 여부를 확인하라. 미연결이면
  gmail_connect_url 을 호출해 실제 Google 로그인 URL을 받은 뒤, **텍스트로 URL을
  그대로 답하지 말고** navigate(targetScreen="GMAIL_CONNECT", label="Gmail
  연동하러 가기", params={"authorizeUrl": 받은 URL}) 를 호출하라(연동 절차는
  이 에이전트가 대신 못 한다). gmail_connect_url 이 {"error": ...} 를 돌려주면
  URL 없이 "잠시 후 다시 시도해달라"고 안내하고 navigate 는 호출하지 마라.
  navigate 호출 후 끝내라.
  연결돼 있으면 gmail_send_email 로 보내되, 받는 사람 이메일 주소가 없으면 지어내지
  말고 ask_user 로 확인하라 — 잘못된 주소로 나간 메일은 되돌릴 수 없다. 메일 본문에는
  반영된 방안(scenarioType)과 핵심 변경 사항을 요약해 담아라.
- 사용자가 메일 발송을 먼저 요청 안 했는데 먼저 나서서 메일을 보내지 마라 — 반영과
  메일 발송은 별개 요청이다."""


_agent = None


async def get_agent():
    """gmail 도구가 async 로만 나오므로(mail 도메인과 동일한 이유 —
    engine_a/domain_agents.py의 get_mail_agent() 참고) 여기서도 직접 캐시를 관리한다.
    gmail-mcp 서버가 죽어 있어 gmail 도구가 하나도 안 붙었으면 캐시하지 않는다 —
    replan 자체(propose/ask_user/save/apply)는 gmail 없이도 완전히 동작하지만, 서버가
    살아난 뒤에도 영영 gmail 없이 굳는 걸 막기 위해서다."""
    global _agent
    if _agent is not None:
        return _agent

    tools = [propose_replan_scenarios, replan_save, ask_user, replan_apply, navigate,
             *await get_gmail_tools()]
    agent = build_domain_agent(
        tools,
        DOMAIN_PROMPT,
        await get_checkpointer(),
        description_prefix="재계획 반영 요청입니다.",
    )
    if any(getattr(t, "name", "").startswith("gmail_") for t in tools):
        _agent = agent
    return agent
