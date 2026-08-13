"""
화면 안내 도구 — NAVIGATE · FILL_FORM (done.action 의 근원)

에이전트가 직접 못 하는 일을 화면으로 넘기는 두 가지 방법:

  navigate   삭제·수정처럼 도구가 없는 작업 → 해당 화면으로 안내 (합의: 삭제는
             NAVIGATE만. 회의록 삭제 = MEETING_DETAIL)
  fill_form  프로젝트 생성처럼 사용자가 최종 버튼을 눌러야 하는 작업 → 폼을
             대신 채워줌 (합의: 프로젝트 생성은 FILL_FORM만 — 에이전트에게
             POST 권한이 없고, 생성 버튼은 반드시 사용자가 누른다)

둘 다 DB 를 건드리지 않으므로 승인(interrupt) 대상이 아니다.
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
def fill_form(targetScreen: str, formData: dict, runtime: ToolRuntime[RunContext]) -> str:
    """화면의 입력 폼을 대신 채운다. 대화로 모은 정보가 충분해졌을 때 호출한다.

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
