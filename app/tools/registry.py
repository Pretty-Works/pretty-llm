"""
도구 레지스트리 — 실행 컨텍스트 · 승인 등급 · 도구 ↔ 내부 API 매핑

여기 한곳에서 정한다:
  ① RunContext   도구가 백엔드를 부를 때 필요한 값 (LLM 이 볼 수 없는 것들)
  ② 승인 등급     auto 모드에서 자동 통과시켜도 되는 도구인가
  ③ 쓰기 도구 명세  도구 인자 → (method, path, params) 변환

★ ③이 중요한 이유
  승인 요청 때 방출한 params 와, 재개 후 실제로 보내는 요청 바디가
  바이트 단위로 같아야 한다(다르면 AGENT_015). 그래서 "도구 인자 → params"
  변환을 이 파일 하나로 모아, 승인 방출 경로와 실행 경로가 같은 함수를 쓰게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass


# ─────────────────────────────────────────────────────────────
# ① 실행 컨텍스트
#    LLM 이 손댈 수 없는 값만 담는다. 도구는 runtime.context 로 읽는다.
#
#    userId 가 없는 이유: 내부 API 는 X-Run-Id 로 백엔드가 요청자를 역산한다.
#    우리가 userId 를 보내면 무시되며, 보낼 수 있게 두면 사칭 경로가 생긴다.
# ─────────────────────────────────────────────────────────────
@dataclass
class RunContext:
    run_id: str                             # X-Run-Id. 체크포인트 키이자 권한 스코프
    approval_token: str | None = None       # WRITE 승인 시 BE 가 발급. resume 바디로 옴
    params_canonical: bytes | None = None   # BE 가 해시한 바이트 원본 (있으면 그대로 전송)

    # done.action 의 출처 — 도구가 실행 결과를 여기 기록하면 SSE 층이 done 에 싣는다.
    #   meeting_create → NAVIGATE(회의록 화면) / navigate → 삭제·수정 안내 /
    #   fill_form → FILL_FORM(폼 채우기). LLM 은 이 필드를 못 본다 (도구만 쓴다).
    action: dict | None = None

    # 대화 요약 훅(memory)용 내부 필드 — 시작 요청에서 채우고 resume 은 체크포인트
    # metadata 에서 복원한다. LLM 은 보지 못한다.
    conversation_id: int | None = None
    goal: str | None = None


# ─────────────────────────────────────────────────────────────
# ② 승인 등급 — auto 모드 정책
#
#    판정 기준: "사용자가 혼자, 흔적 없이 되돌릴 수 있는가?"
#    실제 판정은 백엔드가 하지만, 목록을 여기에도 두어 양 팀이 맞춰볼 근거로 삼는다.
# ─────────────────────────────────────────────────────────────
AUTO_ALLOWED: frozenset[str] = frozenset({
    "meeting.create",            # 삭제 가능 · 알림 없음
    "task.create",
    "task.toggleStatus",         # 토글이라 원복
    "milestone.toggleStatus",
    "replan.save",               # 제안 저장 — 실 데이터 변경 없음
})

AUTO_FORBIDDEN: frozenset[str] = frozenset({
    "leave.create", "leave.update",          # 승인자에게 알림이 이미 감
    "schedule.create", "schedule.update",    # 참석자 전원에게 알림
    "expense.create",                        # 돈
    "replan.apply",                          # 배치 반영 + 여러 구성원에게 알림
})


# ─────────────────────────────────────────────────────────────
# ③ 쓰기 도구 명세
#
#    규칙 1 — 도구 인자 이름 = 내부 API 요청 바디 필드 이름.
#      경로 파라미터만 path_params 로 빼고, 나머지가 그대로 params 가 된다.
#      "인자 → params" 변환에 사람이 개입할 여지가 없어 해시가 어긋나지 않는다.
#
#    규칙 2 — 키는 LangChain 도구 이름(meeting_create), catalog 는 규격 이름(meeting.create).
#      OpenAI 함수명에는 점(.)을 못 쓰므로 둘을 나눈다. 승인 이벤트의 tool 필드와
#      auto 등급표에는 catalog 쪽을 쓴다.
#
#    규칙 3 — 인자에 기본값을 두지 않는다.
#      LLM 이 생략하면 tool_call.args 에 그 키가 빠지는데, 도구 안에서는 기본값이
#      채워져 params 가 달라진다. 규격이 "null 필드는 포함한다(생략과 명시적 null 을
#      구분)"고 못박아 뒀으므로, 선택 항목도 nullable 필수로 두고 LLM 이 null 을
#      명시하게 한다.
# ─────────────────────────────────────────────────────────────
WRITE_TOOLS: dict[str, dict] = {
    "meeting_create": {
        "catalog": "meeting.create",
        "method": "POST",
        "path": "/projects/{projectId}/meetings",
        "path_params": ("projectId",),
    },
    "leave_create": {
        "catalog": "leave.create",          # AUTO_FORBIDDEN — auto 모드에도 항상 사람 승인
        "method": "POST",
        "path": "/leaves",
        "path_params": (),
    },
    "leave_update": {
        "catalog": "leave.update",
        "method": "PATCH",
        "path": "/leaves/{leaveId}",
        "path_params": ("leaveId",),
    },
    "task_create": {
        "catalog": "task.create",           # 배치 — body {"tasks":[...]} 1~10건 단일 트랜잭션
        "method": "POST",
        "path": "/tasks",
        "path_params": (),
    },
    "task_toggle_status": {
        "catalog": "task.toggleStatus",
        "method": "PATCH",
        "path": "/tasks/{taskId}/status",
        "path_params": ("taskId",),
    },
    "schedule_create": {
        "catalog": "schedule.create",
        "method": "POST",
        "path": "/schedules",
        "path_params": (),
    },
    "schedule_update": {
        "catalog": "schedule.update",
        "method": "PATCH",
        "path": "/schedules/{scheduleId}",
        "path_params": ("scheduleId",),
    },
    "expense_create": {
        "catalog": "expense.create",
        "method": "POST",
        "path": "/projects/{projectId}/expenses",
        "path_params": ("projectId",),
    },
    "milestone_toggle_status": {
        "catalog": "milestone.toggleStatus",
        "method": "PATCH",
        "path": "/projects/{projectId}/milestones/{milestoneId}/status",
        "path_params": ("projectId", "milestoneId"),
    },
    "replan_save": {
        "catalog": "replan.save",           # 제안 저장 — 실 데이터 변경 없음(승인 불필요)
        "method": "POST",
        "path": "/projects/{projectId}/replans",
        "path_params": ("projectId",),
    },
    "replan_apply": {
        "catalog": "replan.apply",          # AUTO_FORBIDDEN — 저장분에서 꺼내 반영(승인 필요)
        "method": "POST",
        "path": "/projects/{projectId}/replans/{replanId}/apply",
        "path_params": ("projectId", "replanId"),
    },
}


def build_request(tool_name: str, args: dict) -> tuple[str, str, dict]:
    """도구 인자 → (HTTP 메서드, 내부 API 경로, 요청 바디 params).

    승인 요청을 방출할 때와 실제로 호출할 때 **둘 다 이 함수를 쓴다.**
    한쪽만 다르게 만들면 해시가 어긋나 AGENT_015 가 난다.
    """
    spec = WRITE_TOOLS[tool_name]
    path = spec["path"].format(**args)
    params = {k: v for k, v in args.items() if k not in spec["path_params"]}
    return spec["method"], path, params


def is_write(tool_name: str) -> bool:
    """WRITE 도구인가. approval_request 의 access 필드를 정하는 기준."""
    return tool_name in WRITE_TOOLS


def catalog_name(tool_name: str) -> str:
    """LangChain 도구 이름 → 규격 카탈로그 이름 (meeting_create → meeting.create)."""
    spec = WRITE_TOOLS.get(tool_name)
    return spec["catalog"] if spec else tool_name
