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
