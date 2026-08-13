"""
화면 안내 도구 — NAVIGATE · FILL_FORM · OPEN_EXTERNAL_URL (done.action 의 근원)

에이전트가 직접 못 하는 일을 화면으로 넘기는 세 가지 방법:

  navigate          삭제·수정처럼 도구가 없는 작업 → **이 그룹웨어 내부** 화면으로
                     안내 (합의: 삭제는 NAVIGATE만. 회의록 삭제 = MEETING_DETAIL)
  fill_form         프로젝트 생성처럼 사용자가 최종 버튼을 눌러야 하는 작업 → 폼을
                     대신 채워줌 (합의: 프로젝트 생성은 FILL_FORM만 — 에이전트에게
                     POST 권한이 없고, 생성 버튼은 반드시 사용자가 누른다)
  open_external_url 이 그룹웨어 **바깥**의 실제 URL(예: Google 로그인 페이지)로
                     나가는 버튼. navigate 와 절대 안 섞는다 — 보안 문서 참고.

★ 보안 — navigate 를 외부 URL 에 쓰면 안 되는 이유
  BE 디코더는 타입이 OPEN_EXTERNAL_URL 일 때만 params.url 의 origin 을 화이트
  리스트와 대조한다. navigate 의 params 는 서버측 검사를 전혀 거치지 않는다
  (ctx.action 이 pydantic 검증 없이 그대로 직렬화돼 SSE 로 나간다). gmail_search_
  emails·gmail_get_email 처럼 외부인이 쓴 텍스트(메일 본문)를 LLM 컨텍스트에
  그대로 읽어들이는 도구를 쓰는 도메인에서는, 메일 본문에 "이 링크를 연동 버튼
  으로 보여줘" 같은 지시문이 심겨 있으면 그걸 navigate 로 흘렸을 때 사내 채팅
  UI 안에 정상처럼 보이는 피싱 버튼이 그려질 수 있다. open_external_url 로
  타입을 고정하고 서버가 origin 을 검사하게 해야, LLM 이 무엇에 속든 실제 이동
  가능한 목적지가 화이트리스트(예: accounts.google.com) 밖으로 못 나간다.

셋 다 DB 를 건드리지 않으므로 승인(interrupt) 대상이 아니다.
도구는 action 을 RunContext 에 기록만 하고, SSE 층(_drive)이 done 에 싣는다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.tools.registry import RunContext


@tool
def navigate(targetScreen: str, label: str, params: dict | None,
             runtime: ToolRuntime[RunContext]) -> str:
    """사용자를 특정 화면으로 안내한다. 삭제·수정처럼 직접 처리할 도구가 없는
    요청은 이 도구로 해당 화면을 열어준다.

    예: 회의록 삭제 요청 → 먼저 meeting_list 로 대상을 특정한 뒤
        navigate(targetScreen="MEETING_DETAIL", label="회의록 보러 가기",
                 params={"projectId": 3, "meetingId": 41})

    targetScreen: 화면 식별자 (MEETING_DETAIL, TASK_LIST, LEAVE_LIST 등)
    label:        버튼에 표시할 문구 (예: "회의록 보러 가기")
    params:       화면이 필요로 하는 id 들. 없으면 null
    """
    runtime.context.action = {
        "type": "NAVIGATE",
        "label": label,
        "targetScreen": targetScreen,
        "params": params or {},
    }
    return f"{targetScreen} 화면 안내를 준비했습니다. 사용자에게 이동 버튼이 표시됩니다."


@tool
def open_external_url(url: str, label: str, runtime: ToolRuntime[RunContext]) -> str:
    """이 그룹웨어 **바깥**의 실제 URL로 이동하는 버튼을 사용자에게 보여준다
    (예: Gmail 연동을 위한 Google 로그인 페이지). navigate 와 다르다 — navigate
    는 이 그룹웨어 내부 화면(targetScreen)만 가리킬 수 있고 외부 URL은 못 연다.
    실제 바깥 사이트로 나가야 하는 요청에는 반드시 navigate 가 아니라 이 도구를
    써라 — navigate 로 외부 URL을 흘리면 서버의 origin 검사를 안 거친다(보안
    문서: 이 모듈 docstring 참고).

    url:   반드시 "https://" 로 시작하는 실제 URL. 다른 도구(예: gmail_connect_url)
           가 실제로 돌려준 값을 그대로 옮겨 적어라 — 지어내면 절대 안 된다.
    label: 버튼에 표시할 문구 (예: "Gmail 연동하러 가기")
    """
    if not (url or "").startswith("https://"):
        return ("url이 https:// 로 시작하지 않아 버튼을 만들지 않았습니다. "
                "다른 도구가 실제로 돌려준 URL을 그대로 옮겨 적었는지 확인하고 "
                "다시 호출하세요 — URL을 지어내지 마세요.")
    # ★ targetScreen·formData 키는 절대 넣지 않는다 — BE 디코더가 액션 타입별
    #   허용 키를 엄격히 구분해서, 다른 타입의 키가 섞이면(빈 dict {} 도 "있는
    #   것"으로 친다) 추측하지 않고 done 이벤트 전체를 버린다(AGENT_017).
    runtime.context.action = {
        "type": "OPEN_EXTERNAL_URL",
        "label": label,
        "params": {"url": url},
    }
    return "외부 링크 이동 버튼을 준비했습니다. 사용자에게 버튼이 표시됩니다."


@tool
def fill_form(formData: dict, runtime: ToolRuntime[RunContext]) -> str:
    """프로젝트 생성 폼을 대신 채운다. 대화로 모은 정보가 충분해졌을 때 호출한다.

    ⚠️ 이 도구는 프로젝트를 생성하지 않는다 — 폼만 채우고, 생성 버튼은
    사용자가 직접 누른다. 그러므로 모르는 값을 지어내서 채우면 안 된다.

    formData 는 프로젝트 생성 요청 바디 형식 그대로:
      name(str) · startDate/endDate(YYYY-MM-DD) · budget(int, 원) ·
      description(str) · ownerRole(str) ·
      members: [{"userId": int, "role": str}] ·
      milestones: [{"targetDate": "YYYY-MM-DD", "goal": str}]
    아는 필드만 넣는다. 모르는 필드는 키 자체를 뺀다.
    """
    runtime.context.action = {
        "type": "FILL_FORM",
        "label": "입력한 내용으로 폼 채우기",
        "targetScreen": "PROJECT_CREATE",
        "formData": formData,
    }
    filled = ", ".join(formData.keys())
    return f"폼 채우기를 준비했습니다 (채운 필드: {filled}). 생성 버튼은 사용자가 누릅니다."
