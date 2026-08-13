"""
도구 레지스트리 — 실행 컨텍스트 · 승인 등급 · 도구 ↔ 내부 API 매핑

여기 한곳에서 정한다:
  ① RunContext   도구가 백엔드를 부를 때 필요한 값 (LLM 이 볼 수 없는 것들)
  ② 승인 등급     auto 모드에서 자동 통과시켜도 되는 도구인가
  ③ 쓰기 도구 명세  도구 인자 → (method, path, params) 변환 (BE 내부 API 도구만)
  ④ MCP 쓰기 도구  BE 내부 API가 없는 쓰기 도구(gmail_send_email 등)도 같은 승인
                    게이트를 타게 한다

★ ③이 중요한 이유
  승인 요청 때 방출한 params 와, 재개 후 실제로 보내는 요청 바디가
  바이트 단위로 같아야 한다(다르면 AGENT_015). 그래서 "도구 인자 → params"
  변환을 이 파일 하나로 모아, 승인 방출 경로와 실행 경로가 같은 함수를 쓰게 한다.

★ ④가 필요한 이유
  `app/engine_a/domain_agents.py`의 `build_domain_agent()`는 "쓰기 도구는
  `is_write()`로 자동 판별해 승인 게이트를 건다"는 원칙으로 짜여 있는데, 그
  `is_write()`가 지금까지는 WRITE_TOOLS(= BE 내부 API로 나가는 도구)만 봤다.
  gmail_send_email처럼 BE가 아니라 MCP 서버(mcp_servers/gmail_mcp)로 나가는
  쓰기 도구는 method/path가 없어 WRITE_TOOLS 형식에 안 맞는다 — 그렇다고 그냥
  두면 gmail 도구가 언젠가 에이전트에 묶이는 순간 승인 없이 자동 실행되는
  구멍이 조용히 생긴다. MCP_WRITE_TOOLS는 그 구멍을 막는 두 번째 목록이고,
  `is_write()`가 이 둘을 합쳐서 판정하므로 domain_agents.py 쪽은 수정할
  필요가 없다("빠뜨리는 사고 원천 차단" 원칙 그대로 유지).
"""

from __future__ import annotations

from dataclasses import dataclass, field


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
    # 채팅에 첨부된 파일 [{name, content}] — BE 가 텍스트 추출해 runs 바디로 보낸다.
    attachments: list[dict] | None = None

    # ★ 8/12 추가 — 이번 turn에서 읽기 도구가 이미 조회한 사실(사람이 읽는
    #   요약 문자열)의 캐시. analyze_impact 가 엔진 B 를 부르기 직전에 이걸
    #   질문에 그대로 이어붙인다 — LLM 이 요약하다 놓치는 걸 막기 위해서다.
    #   LLM 은 이 필드를 못 보고, 도구만 채우고 읽는다.
    known_facts: dict[str, str] = field(default_factory=dict)


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
    "task.update",               # 본인 할일 내용/마감/프로젝트 수정 — 다시 고칠 수 있음
    "milestone.toggleStatus",
    # ★ replan.create(저장)는 여기 없다 — 8/9 BE 스펙 개정으로 AUTO_FORBIDDEN 으로
    #   옮겨갔다(아래). 예전엔 여기 있었는데, 옮기면서 지우지 않아 두 목록에 동시에
    #   있던 걸 정리했다 — 있으면 auto 모드 판정이 어느 쪽을 봐야 할지 애매해진다.
})

AUTO_FORBIDDEN: frozenset[str] = frozenset({
    "leave.create", "leave.update",          # 승인자에게 알림이 이미 감
    "schedule.create", "schedule.update",    # 참석자 전원에게 알림
    "expense.create",                        # 돈
    "replan.create",                         # ★ 2026-08-09 BE 스펙 개정 — 저장도 승인 필요.
                                              #   외부 텍스트를 읽고 만든 계획이 "공식 기록"이
                                              #   되는 시점이라 사람이 한 번 본다.
                                              #   ★ 8/12 — 카탈로그 이름을 "replan.save"에서
                                              #   BE 컨트롤러 상수/렌더러가 실제로 아는
                                              #   "replan.create"로 맞췄다. 이름이 안 맞으면
                                              #   BE 가 승인 카드를 못 그리거나(§ApprovalPreviewRegistry)
                                              #   재개 검증에서 걸려 AGENT_007 로 뭉개진다.
    "replan.apply",                          # 배치 반영 + 여러 구성원에게 알림
    "gmail.send",                            # 상대방에게 실제 메일이 나감 — 되돌릴 수 없음
})


# ─────────────────────────────────────────────────────────────
# ③ 쓰기 도구 명세
#
#    규칙 1 — 도구 인자 이름 = 내부 API 요청 바디 필드 이름.
#      인자 전부가 그대로 params(요청 바디)가 된다. ★ path_params 는 항상 () 로
#      둔다 — 경로변수(projectId 등)도 바디에 남아야 한다. 승인 해시가 바디만
#      덮으므로 바디에서 빼면 승인의 보호를 못 받고, BE 승인 카드 렌더러도
#      바디에서 이 값들을 필수로 읽는다(없으면 AGENT_007). 값을 넣는 순간
#      그 도구는 죽는다 — 8/12에 쓰기 8종이 이걸로 전멸했었다.
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
        "path_params": (),
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
        "path_params": (),
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
        "path_params": (),
    },
    "task_update": {
        "catalog": "task.update",           # PUT 전체 교체 — content/projectId/dueDate 항상 셋 다 보낸다
        "method": "PUT",                    # ⚠️ BE 에 이 엔드포인트가 아직 없다 — 합의 대기
        "path": "/tasks/{taskId}",
        "path_params": (),
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
        "path_params": (),
    },
    "expense_create": {
        "catalog": "expense.create",
        "method": "POST",
        "path": "/projects/{projectId}/expenses",
        "path_params": (),
    },
    "milestone_toggle_status": {
        "catalog": "milestone.toggleStatus",
        "method": "PATCH",
        "path": "/projects/{projectId}/milestones/{milestoneId}/status",
        "path_params": (),
    },
    "replan_save": {
        # ★ 8/12 — 이 딕셔너리가 "catalog"/"method"/"path"/"path_params" 를 두 번
        #   정의하고 있었다(파이썬은 문법 오류 없이 뒤 블록으로 조용히 덮어쓴다).
        #   그래서 실제로는 뒤 블록의 path_params=("projectId",) 가 적용돼
        #   build_request() 가 projectId 를 바디에서 빼먹고 있었다 — BE 가 바디에도
        #   projectId 를 기대하면 그대로 400 → AGENT_007. 아래가 원래 의도대로
        #   하나로 합친 버전이다.
        "catalog": "replan.create",         # AUTO_FORBIDDEN — 승인 필요(2026-08-09 스펙 개정)
                                             #   ★ BE 컨트롤러 상수/렌더러가 아는 이름은
                                             #   "replan.save" 가 아니라 "replan.create" 다.
        "method": "POST",
        "path": "/projects/{projectId}/replans",
        # ★ path_params 를 비워둔다 — projectId 가 경로에도, 바디에도 그대로 들어가야
        #   한다(승인 토큰이 바디 해시로 봉인되므로, 경로만으로 대상을 정하면 승인 때와
        #   다른 프로젝트로 보내도 해시가 그대로다). path.format(**args) 는 args 에 있는
        #   키만 쓰므로 projectId 가 params 에도 남아 있어도 문제없다.
        "path_params": (),
    },
    "replan_apply": {
        "catalog": "replan.apply",          # AUTO_FORBIDDEN — 저장분에서 꺼내 반영(승인 필요)
        "method": "POST",
        "path": "/projects/{projectId}/replans/{replanId}/apply",
        # ★ 위와 동일한 이유로 projectId·replanId 둘 다 body 에도 남긴다.
        "path_params": (),
    },
}


# ─────────────────────────────────────────────────────────────
# ④ MCP 쓰기 도구 — BE 내부 API가 아니라 MCP 서버(gmail-mcp 등)로 나가는
#    쓰기 도구. method/path가 없어 WRITE_TOOLS 형식(③)을 못 쓰지만, "auto
#    모드에서도 사람 승인이 필요하다"는 성격은 BE 쓰기 도구와 같다.
#    catalog 이름만 따로 매핑해 둔다(승인 카드 표시용, AUTO_FORBIDDEN과 짝).
# ─────────────────────────────────────────────────────────────
MCP_WRITE_TOOLS: dict[str, dict] = {
    "gmail_send_email": {
        "catalog": "gmail.send",   # AUTO_FORBIDDEN — 상대방에게 실제 메일이 나가는 되돌릴 수 없는 행동
    },
}


def build_request(tool_name: str, args: dict) -> tuple[str, str, dict]:
    """도구 인자 → (HTTP 메서드, 내부 API 경로, 요청 바디 params). BE 내부 API 도구 전용.

    승인 요청을 방출할 때와 실제로 호출할 때 **둘 다 이 함수를 쓴다.**
    한쪽만 다르게 만들면 해시가 어긋나 AGENT_015 가 난다.

    MCP 쓰기 도구(gmail_send_email 등)는 method/path가 없어 여기 못 들어온다 —
    호출 전에 `is_mcp_write()`로 걸러야 한다(app/common/hitl.py 참고).
    """
    spec = WRITE_TOOLS[tool_name]
    path = spec["path"].format(**args)
    params = {k: v for k, v in args.items() if k not in spec["path_params"]}
    return spec["method"], path, params


def is_write(tool_name: str) -> bool:
    """WRITE 도구인가. approval_request 의 access 필드 + 승인 게이트(interrupt_on)를
    정하는 기준. BE 내부 API 도구(WRITE_TOOLS)와 MCP 쓰기 도구(MCP_WRITE_TOOLS)
    둘 다 포함한다 — 어느 쪽이든 사람 승인 없이 자동 실행되면 안 되는 건 같다."""
    return tool_name in WRITE_TOOLS or tool_name in MCP_WRITE_TOOLS


def is_mcp_write(tool_name: str) -> bool:
    """MCP 서버로 나가는 쓰기 도구인가 — build_request()를 쓸 수 없는 도구.
    approval 카드를 만드는 쪽(app/common/hitl.py)이 build_request() 대신
    이 함수로 분기해야 KeyError 없이 승인 카드를 만들 수 있다."""
    return tool_name in MCP_WRITE_TOOLS


def catalog_name(tool_name: str) -> str:
    """LangChain 도구 이름 → 규격 카탈로그 이름 (meeting_create → meeting.create)."""
    spec = WRITE_TOOLS.get(tool_name) or MCP_WRITE_TOOLS.get(tool_name)
    return spec["catalog"] if spec else tool_name
