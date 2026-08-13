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


# 화면별 필수 params — 채우지 않으면 FE 가 경로를 못 만들어 이동 카드를 **조용히 버린다**
#   (FE screenRegistry.ts 의 pattern 에 [projectId] 같은 자리가 있는 화면들.
#    실사용 사고: TASK_LIST 를 params={} 로 보내 "이동에 필요한 값이 비어 카드를
#    띄우지 않음" 이 뜨고 사용자에겐 아무 버튼도 안 보였다.)
#   ★ TASK_LIST·LEAVE_LIST·SCHEDULE_LIST 는 FE 에 전용 화면이 없어 별칭으로 풀린다:
#     TASK_LIST → 프로젝트 개요(projectId 필요) / LEAVE_LIST·SCHEDULE_LIST → 캘린더(불필요)
_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "PROJECT_OVERVIEW": ("projectId",),
    "PROJECT_DETAIL": ("projectId",),        # 별칭 → PROJECT_OVERVIEW
    "TASK_LIST": ("projectId",),             # 별칭 → PROJECT_OVERVIEW
    "PROJECT_EDIT": ("projectId",),
    "BOARD_LIST": ("projectId",),
    "BOARD_WRITE": ("projectId",),
    "BOARD_DETAIL": ("projectId", "postId"),
    "MEETING_LIST": ("projectId",),
    "MEETING_CREATE": ("projectId",),
    "MEETING_DRAFT": ("projectId",),         # 별칭 → MEETING_CREATE
    "MEETING_DETAIL": ("projectId", "meetingId"),
    "FINANCE": ("projectId",),
}


@tool
def navigate(targetScreen: str, label: str, params: dict | None,
             runtime: ToolRuntime[RunContext]) -> str:
    """사용자를 특정 화면으로 안내한다. 삭제·수정처럼 직접 처리할 도구가 없는
    요청은 이 도구로 해당 화면을 열어준다.

    예: 회의록 삭제 요청 → 먼저 meeting_list 로 대상을 특정한 뒤
        navigate(targetScreen="MEETING_DETAIL", label="회의록 보러 가기",
                 params={"projectId": 3, "meetingId": 41})

    ★ 화면마다 **반드시 채워야 하는 params** 가 있다. 빠뜨리면 사용자 화면에
      이동 버튼이 아예 안 뜬다(조용히 버려진다).
        projectId 필요 : PROJECT_OVERVIEW · TASK_LIST · MEETING_LIST ·
                         MEETING_CREATE · BOARD_LIST · FINANCE · PROJECT_EDIT
        projectId+meetingId : MEETING_DETAIL
        params 불필요  : CALENDAR · LEAVE_LIST · SCHEDULE_LIST · PROJECT_CREATE · HOME
      projectId 를 모르면 지어내지 말고 project_search 로 먼저 찾아라.

    targetScreen: 화면 식별자 (MEETING_DETAIL, TASK_LIST, LEAVE_LIST 등)
    label:        버튼에 표시할 문구 (예: "회의록 보러 가기")
    params:       화면이 필요로 하는 id 들. 필요 없는 화면이면 null
    """
    given = params or {}
    missing = [p for p in _REQUIRED_PARAMS.get(targetScreen, ()) if not given.get(p)]
    if missing:
        # 여기서 끊지 않으면 action 이 만들어져도 FE 가 버려서 사용자는 아무것도 못 본다.
        return (f"{targetScreen} 화면으로 이동하려면 params 에 {', '.join(missing)} 이(가) "
                f"필요합니다 — 지금 값이 비어 있어 이동 버튼을 만들지 못했습니다. "
                f"project_search 등으로 실제 값을 확인한 뒤 다시 호출하세요. "
                f"(지어내지 마세요)")

    runtime.context.action = {
        "type": "NAVIGATE",
        "label": label,
        "targetScreen": targetScreen,
        "params": given,
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
def fill_form(targetScreen: str, formData: dict, runtime: ToolRuntime[RunContext]) -> str:
    화면의 입력 폼을 대신 채운다. 대화로 모은 정보가 충분해졌을 때 호출한다.

    ⚠️ 이 도구는 아무것도 저장·생성하지 않는다 — 폼만 채우고, 최종 버튼은
    사용자가 직접 누른다. 그러므로 모르는 값을 지어내서 채우면 안 된다.

    ★ 8/13 수정 — 전에는 targetScreen 이 "PROJECT_CREATE" 로 고정돼 있어서
      프로젝트 생성 화면 말고는 이 도구를 써도 FE 가 반응하지 않았다(다른 화면은
      다른 targetScreen 을 기다리고 있어서 매칭이 안 됨 — 실사용 사고 사례).
      이제 호출하는 쪽이 지금 사용자가 보고 있는 화면에 맞는 값을 넣어야 한다.

    targetScreen: 지금 채울 화면 식별자. 대화의 "(현재 화면: ...)" 값을 그대로
      쓰거나, 문맥상 이동해야 할 화면이면 그 화면의 식별자를 쓴다.
      (예: PROJECT_CREATE)
    formData: 그 화면이 기대하는 필드만 담는다 — 화면마다 형태가 다르므로
      프로젝트 생성이면 name·startDate/endDate·budget·description·ownerRole·
      members·milestones, 다른 화면이면 그 화면의 필드를 쓴다. 아는 필드만
      넣고 모르는 필드는 키 자체를 뺀다.
    """
    runtime.context.action = {
        "type": "FILL_FORM",
        "label": "입력한 내용으로 폼 채우기",
        "targetScreen": targetScreen,
        "formData": formData,
    }
    filled = ", ".join(formData.keys())
    return f"폼 채우기를 준비했습니다 (채운 필드: {filled}). 최종 버튼은 사용자가 누릅니다."
