# app/schemas/state.py
"""Engine B 공용 스키마 / LangGraph 상태 정의.

여기 있는 모델이 Engine B 안에서 오가는 **유일한** 계약이다.
- Analysis Router -> `AnalysisPlan`
- Context Builder -> `AnalysisContext`
- 모든 Worker    -> `WorkerOutput` (dimension / result / reasoning / confidence 공통)
- Validator      -> `ValidationReport`
- Synthesis      -> `SynthesisResult`

담당자 3(Scenario Executor / Tradeoff)도 이 스키마를 그대로 쓴다.
조정안 하나 = `ScenarioSpec` 하나이고, 워커 출력은 `scenario_id` 로 구분된다.
"""

import operator
from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field


# ─── 공통 열거형 ──────────────────────────────────────────────────
#
# StrEnum 이라 문자열과 그대로 비교되고(`d == "project"`), f-string 도 값으로 찍힌다.
# 일반 Enum 이면 f"{Domain.project}" 가 "Domain.project" 로 나와 노드 이름이 깨진다.

class Domain(StrEnum):
    """분석 도메인. request.py · response.py 와 공유하는 어휘다."""

    project = "project"    # 담당자 2
    hcm = "hcm"            # 담당자 2
    me = "me"              # 본인 스코프 전용 — 내 할 일·일정·잔여연차만 본다
    meeting = "meeting"    # 담당자 3
    vacation = "vacation"  # Engine A 에서 위험 신호가 잡혔을 때 넘어온다
    expense = "expense"    # 어휘에만 있고 Engine B 워커는 없다


class Mode(StrEnum):
    analysis = "analysis"      # 현황 분석 · 결론 도출
    derivation = "derivation"  # 후보 생성 (인력 추천, 회의 슬롯). 라우팅은 아직 없다
    replan = "replan"          # 조정안 여러 개를 만들어 비교 (Scenario Executor 경로)


class Route(StrEnum):
    """Orchestrator 라우팅. Engine B 는 engine_b 만 받는다."""

    simple_query = "simple_query"
    engine_a = "engine_a"
    engine_b = "engine_b"


Severity = Literal["error", "warning"]

# 워커 1개가 담당하는 분석 축. 도메인별 워커 목록은 app/workers/registry.py 참고.
Dimension = str


# ─── 입력 ─────────────────────────────────────────────────────────

class UIContext(BaseModel):
    """사용자가 어느 화면에서 물어봤는지. 라우터가 대명사를 해석할 때 쓴다."""

    screen: str | None = None  # "project_detail", "calendar", ...
    project_id: int | None = None
    task_id: int | None = None


class AuthContext(BaseModel):
    """백엔드가 인증을 끝낸 뒤 넘겨주는 호출자 정보.

    AI 서버는 토큰을 검증하지 않는다. 소속 검증은 백엔드 몫.
    """

    user_id: int


class AnalysisRequest(BaseModel):
    """Orchestrator -> Engine B 진입 입력."""

    query: str
    user_id: int
    chat_history: list[dict[str, str]] = Field(default_factory=list)
    ui_context: UIContext = Field(default_factory=UIContext)
    as_of: date | None = None

    @property
    def auth(self) -> AuthContext:
        """Worker·Tool 로 내려보낼 인증 컨텍스트."""
        return AuthContext(user_id=self.user_id)


# ─── Analysis Router 출력 ─────────────────────────────────────────

class Entities(BaseModel):
    """질의에서 뽑아낸 대상. 값이 없으면 Context Builder 가 채우거나 워커가 툴로 찾는다."""

    project_ids: list[int] = Field(default_factory=list)
    project_names: list[str] = Field(default_factory=list)
    user_ids: list[int] = Field(default_factory=list)
    user_names: list[str] = Field(default_factory=list)
    task_ids: list[int] = Field(default_factory=list)
    date_from: date | None = None
    date_to: date | None = None
    # "예산 2천만 더", "인원 2명 빼고" 같은 조정 폭
    budget_delta: int | None = None
    headcount_delta: int | None = None


class AnalysisPlan(BaseModel):
    """Analysis Router 의 LLM 1콜 결과.

    중요: `focus` 는 **강조점**일 뿐 실행 여부와 무관하다.
    선택된 도메인의 워커 세트는 항상 전부 병렬 실행된다.
    """

    mode: Mode = Mode.analysis
    domains: list[Domain] = Field(default_factory=lambda: [Domain.project])
    focus: list[Dimension] = Field(default_factory=list)
    objective: str = ""
    entities: Entities = Field(default_factory=Entities)
    constraints: list[str] = Field(default_factory=list)
    reasoning: str = ""
    confidence: float = 0.5


# ─── Context Builder 출력 ─────────────────────────────────────────

class MemberSnapshot(BaseModel):
    """프로젝트 참여자. 내부도구가 주는 필드만 담는다.

    입사일(hire_date)은 넣지 않는다 — BE 응답에 없고(AgentMemberListResponse 는
    department·position 뿐), 근속 기반 역량 추정 자체를 하지 않기로 했다.
    """

    user_id: int
    name: str
    department: str | None = None
    position: str | None = None
    role: str | None = None  # 프로젝트 내 역할 (PM / BE / FE / QA ...)
    status: str | None = None


class TodoSnapshot(BaseModel):
    id: int
    project_id: int | None = None
    title: str = ""
    status: str = "TODO"  # TODO / IN_PROGRESS / DONE / CANCELED
    due_date: date | None = None
    assignee_id: int | None = None
    assignee_name: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status in {"TODO", "IN_PROGRESS"}


class MyWeekSnapshot(BaseModel):
    """요청자 본인의 주간 스냅샷.

    전부 본인 스코프 내부도구다 (`/me`, `/tasks`, `/schedules`, `/leaves/balance`).
    남의 데이터가 섞일 여지가 없어 이 축은 개인정보 판단이 필요 없다.
    """

    week_start: date | None = None
    week_end: date | None = None
    tasks: list[TodoSnapshot] = Field(default_factory=list)
    schedules: list[dict[str, Any]] = Field(default_factory=list)
    leave_granted_days: int | None = None
    leave_used_days: int | None = None
    leave_remaining_days: int | None = None

    @property
    def open_tasks(self) -> list[TodoSnapshot]:
        return [t for t in self.tasks if t.is_open]


class MilestoneSnapshot(BaseModel):
    """마일스톤 1건. 일정 판단의 1차 근거다 — 할 일과 달리 기간 제약 없이 전체를 받는다."""

    id: int
    goal: str = ""
    target_date: date | None = None
    completed: bool = False
    is_overdue: bool = False
    is_next: bool = False


class MeetingSnapshot(BaseModel):
    """회의록 1건. content·follow_up 은 최근 몇 건만 채운다(토큰 절약)."""

    id: int
    title: str = ""
    meeting_date: date | None = None
    purpose: str | None = None
    content: str | None = None
    follow_up: str | None = None
    attendee_names: list[str] = Field(default_factory=list)


class ProjectSnapshot(BaseModel):
    id: int
    name: str
    status: str = "ACTIVE"
    start_date: date | None = None
    due_date: date | None = None
    members: list[MemberSnapshot] = Field(default_factory=list)
    todos: list[TodoSnapshot] = Field(default_factory=list)
    milestones: list[MilestoneSnapshot] = Field(default_factory=list)
    meetings: list[MeetingSnapshot] = Field(default_factory=list)

    @property
    def milestone_progress(self) -> float | None:
        """완료 마일스톤 비율. 마일스톤이 없으면 None — 할 일 비율로 대신하지 않는다."""
        if not self.milestones:
            return None
        done = sum(1 for m in self.milestones if m.completed)
        return round(done / len(self.milestones), 3)

    @property
    def overdue_milestones(self) -> list[MilestoneSnapshot]:
        return [m for m in self.milestones if m.is_overdue and not m.completed]

    @property
    def next_milestone(self) -> MilestoneSnapshot | None:
        return next((m for m in self.milestones if m.is_next), None)

    @property
    def open_todos(self) -> list[TodoSnapshot]:
        return [t for t in self.todos if t.is_open]

    @property
    def progress(self) -> float:
        """DONE 비율. 취소된 할 일은 분모에서 뺀다."""
        counted = [t for t in self.todos if t.status != "CANCELED"]
        if not counted:
            return 0.0
        done = sum(1 for t in counted if t.status == "DONE")
        return round(done / len(counted), 3)


class BudgetSnapshot(BaseModel):
    project_id: int
    total: int = 0
    spent: int = 0
    committed: int = 0  # 결재 진행 중이라 아직 안 나갔지만 묶여 있는 금액
    currency: str = "KRW"

    @property
    def remaining(self) -> int:
        return self.total - self.spent - self.committed

    @property
    def usage_ratio(self) -> float:
        if self.total <= 0:
            return 0.0
        return round((self.spent + self.committed) / self.total, 3)


class LeaveSnapshot(BaseModel):
    id: int
    user_id: int
    user_name: str | None = None
    leave_type: str = "연차"
    start_date: date | None = None
    end_date: date | None = None
    status: str = "APPROVED"


class AnalysisContext(BaseModel):
    """워커들이 공통으로 보는 사실 묶음.

    워커마다 같은 데이터를 다시 긁지 않도록 여기서 한 번만 모은다.
    못 가져온 항목은 `missing` 에 남기고, 워커가 필요하면 툴로 직접 채운다.
    """

    as_of: date
    requester: MemberSnapshot | None = None
    projects: list[ProjectSnapshot] = Field(default_factory=list)
    budgets: list[BudgetSnapshot] = Field(default_factory=list)
    leaves: list[LeaveSnapshot] = Field(default_factory=list)
    # 후보군은 프로젝트 참여자 + 질문에 이름이 나온 사람뿐이다. 전사 명부는 받지 않는다
    # (BE 가 /users 에 keyword 를 필수로 걸어 막아둔 경로다).
    candidates: list[MemberSnapshot] = Field(default_factory=list)
    # Data Gate 가 근거 부족으로 건너뛴 축. 그대로 답변의 "확인 못한 것"이 된다.
    skipped: list[str] = Field(default_factory=list)
    # 본인 스코프 질문("뭐부터 할까")일 때만 채워진다. me 도메인 전용.
    my_week: "MyWeekSnapshot | None" = None
    # context_builder._availability() 결과. 셀 수 있는 가용성 지표는
    # LLM 이 아니라 코드가 계산해서 넣는다. (hcm 도메인일 때만 채워진다)
    workloads: list[dict[str, Any]] = Field(default_factory=list)
    window_from: date | None = None
    window_to: date | None = None
    notes: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    def project(self, project_id: int | None = None) -> ProjectSnapshot | None:
        if project_id is None:
            return self.projects[0] if self.projects else None
        return next((p for p in self.projects if p.id == project_id), None)

    def budget(self, project_id: int) -> BudgetSnapshot | None:
        return next((b for b in self.budgets if b.project_id == project_id), None)

    def member(self, user_id: int) -> MemberSnapshot | None:
        for project in self.projects:
            for member in project.members:
                if member.user_id == user_id:
                    return member
        return next((c for c in self.candidates if c.user_id == user_id), None)

    # LLM 이 뱉는 참조는 "todo:101" 같은 문자열 라벨이라 비교용으로 문자열 집합을 준다.
    def known_user_ids(self) -> set[str]:
        ids = {str(c.user_id) for c in self.candidates}
        for project in self.projects:
            ids.update(str(m.user_id) for m in project.members)
        return ids

    def known_todo_ids(self) -> set[str]:
        return {str(t.id) for p in self.projects for t in p.todos}

    def known_milestone_ids(self) -> set[str]:
        return {str(m.id) for p in self.projects for m in p.milestones}


# ─── 재계획(조정안) - 담당자 3 이 채워 넣는 부분 ──────────────────

class ScenarioSpec(BaseModel):
    """조정안 1개. Scenario Executor 가 3개를 만들어 병렬로 돌린다."""

    scenario_id: str = "base"
    label: str = "기준안"
    description: str = ""
    # 이 조정안이 컨텍스트에 가하는 변경 (마감 +2주, 인원 -1 등)
    overrides: dict[str, Any] = Field(default_factory=dict)


# ─── Worker 출력 (모든 워커 공통) ─────────────────────────────────

class Evidence(BaseModel):
    """결론의 근거 한 줄. 어떤 데이터에서 나왔는지 추적 가능해야 한다."""

    source: Literal["context", "tool"] = "context"
    ref: str = ""  # "project:1001", "tool:list_project_tasks"
    detail: str = ""


class WorkerOutput(BaseModel):
    """워커 공통 출력 계약: dimension / result / reasoning / confidence."""

    dimension: Dimension
    domain: Domain | None = None  # 담당자3 meeting 에이전트는 안 채운다
    result: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    scenario_id: str = "base"
    tool_calls: int = 0
    attempt: int = 1
    error: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.scenario_id, self.dimension)


# 담당자3 쪽에서 쓰는 이름
AgentOutput = WorkerOutput


# ─── Validator 출력 ───────────────────────────────────────────────

class Violation(BaseModel):
    code: str  # DATE_ORDER / BUDGET_EXCEEDED / MEMBER_UNAVAILABLE ...
    severity: Severity = "error"
    dimension: Dimension | None = None  # 어느 워커를 다시 돌릴지
    scenario_id: str = "base"
    subject: str = ""  # 문제가 된 대상 (task id, user id 등)
    message: str = ""
    fix_hint: str = ""  # 재생성 시 워커에게 그대로 전달되는 수정 지시


class ValidationReport(BaseModel):
    violations: list[Violation] = Field(default_factory=list)
    checked: list[str] = Field(default_factory=list)  # 수행한 검사 목록

    @property
    def errors(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def dimensions_to_retry(self) -> list[str]:
        seen: list[str] = []
        for violation in self.errors:
            if violation.dimension and violation.dimension not in seen:
                seen.append(violation.dimension)
        return seen

    def dimensions_with_errors(self) -> set[str]:
        """error 가 남은 축. 재시도로도 못 고쳤으면 통합에서 제외된다."""
        return {v.dimension for v in self.errors if v.dimension}

    def feedback_by_dimension(self) -> dict[str, list[str]]:
        feedback: dict[str, list[str]] = {}
        for violation in self.errors:
            if not violation.dimension:
                continue
            line = f"[{violation.code}] {violation.message} → {violation.fix_hint}"
            feedback.setdefault(violation.dimension, []).append(line)
        return feedback


# ─── Synthesis 출력 ───────────────────────────────────────────────

class ProposedChange(BaseModel):
    """DB 를 바꾸자는 제안. HITL Gate(담당자1)가 승인 단위로 쓴다."""

    kind: str  # todo_update / assignment / schedule / budget ...
    target: str  # "todo:12"
    summary: str = ""
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)


class RecommendedAction(BaseModel):
    order: int = 1
    what: str = ""
    who: str | None = None
    when: str | None = None
    why: str = ""
    source_dimensions: list[Dimension] = Field(default_factory=list)
    confidence: float = 0.5


class Conflict(BaseModel):
    """워커끼리 엇갈린 지점과 그 해소 근거."""

    between: list[Dimension] = Field(default_factory=list)
    issue: str = ""
    resolution: str = ""
    rationale: str = ""


class SynthesisResult(BaseModel):
    """조정안 1개(또는 분석 1건)에 대한 최종 통합 결과."""

    scenario_id: str = "base"
    headline: str = ""
    summary: str = ""
    actions: list[RecommendedAction] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    proposed_changes: list[ProposedChange] = Field(default_factory=list)
    confidence: float = 0.5
    # 검증에서 걸렸지만 재생성으로 못 없앤 잔여 위반 (사용자에게 그대로 보여준다)
    unresolved_violations: list[Violation] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    meeting_ranking: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def requires_approval(self) -> bool:
        """DB 를 바꾸는 제안이 하나라도 있으면 HITL Gate 를 거쳐야 한다."""
        return bool(self.proposed_changes)

    


# ─── LangGraph 상태 ───────────────────────────────────────────────

class TraceEvent(BaseModel):
    """실행 흐름 기록. 데모/디버깅에서 어떤 노드가 어떤 판단을 했는지 보여준다."""

    node: str
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


def merge_worker_outputs(
    left: list[WorkerOutput] | None, right: list[WorkerOutput] | None
) -> list[WorkerOutput]:
    """(scenario_id, dimension) 기준으로 덮어쓰는 리듀서.

    단순 append 를 쓰면 Validator 재생성 결과가 기존 출력과 같이 남아버린다.
    같은 축을 다시 돌리면 새 결과가 이전 결과를 대체해야 한다.
    """
    merged: dict[tuple[str, str], WorkerOutput] = {}
    for output in list(left or []) + list(right or []):
        merged[output.key] = output
    return list(merged.values())


class EngineBState(TypedDict, total=False):
    """Engine B 전체 그래프 상태."""

    request: AnalysisRequest
    plan: AnalysisPlan
    context: AnalysisContext
    scenario: ScenarioSpec
    worker_outputs: Annotated[list[WorkerOutput], merge_worker_outputs]
    validation: ValidationReport
    retry_count: int
    # Validator 가 "이 축들을 다시 돌려라"라고 지목한 목록.
    # 비어 있으면 통합 단계로 넘어간다. (라우팅 함수는 상태를 못 바꾸므로
    # 재시도 판단은 Validator 노드에서 한 번만 하고 그 결과를 여기 담는다)
    retry_dimensions: list[str]
    feedback: dict[str, list[str]]
    result: SynthesisResult
    trace: Annotated[list[TraceEvent], operator.add]


# ─── 담당자 1 · 3 의 공용 계약 ────────────────────────────────────
# main 에 머지된 정의. 바꾸면 workers/meeting, scenario_executor 가 깨진다.

class RouteDecision(BaseModel):
    """Orchestrator 라우팅 결정. Engine B 는 route=engine_b 만 받는다."""

    route: Route
    domain: Domain | None = None
    mode: Mode | None = None
    focus: str | None = None


class ScheduleAgentInput(BaseModel):
    participant_ids: list[int]
    project_id: int
    duration_minutes: int
    from_date: str  # YYYY-MM-DD
    to_date: str  # YYYY-MM-DD


class MeetingSlot(BaseModel):
    start: str  # ISO 8601
    end: str
    reason: str


class ScheduleAgentResult(BaseModel):
    slots: list[MeetingSlot]  # 최대 3개


class ReplanningInput(BaseModel):
    project_id: int
    problem: str


class ScenarioResult(BaseModel):
    scenario_type: str  # 일정연장 / 인력추가 / 범위축소
    changes: list[str]
    expected_outcome: str
    risks: list[str]
    agent_outputs: list[WorkerOutput]


class ScenarioExecutorOutput(BaseModel):
    project_id: int
    problem: str
    scenarios: list[ScenarioResult]  # 항상 3개


class WorkerPayload(TypedDict, total=False):
    """Send() 로 워커 노드에 전달되는 입력.

    워커는 전체 상태가 아니라 자기 일에 필요한 것만 받는다.
    """

    plan: AnalysisPlan
    context: AnalysisContext
    scenario: ScenarioSpec
    dimension: Dimension
    feedback: list[str]
    attempt: int


# ─── Tradeoff (담당자3) ──────────────────────────────────────
#     Replanning 최종 단계. 여러 시나리오를 축별로 비교하고 '추천안'을 낸다.
#     ★ 추천이지 확정이 아니다 — 결과는 HITL 제안으로 나가고 실제 반영은 승인 이후.
class ScenarioComparison(BaseModel):
    scenario_type: str                       # 일정연장 / 인력추가 / 범위축소
    schedule_recovery: str = ""              # 일정 회복 효과: 높음 / 중간 / 낮음
    cost_impact: str = ""                    # 비용 부담:      높음 / 중간 / 낮음
    risk_level: str = ""                     # 리스크:         높음 / 중간 / 낮음
    summary: str = ""                        # 한 줄 요약


class TradeoffResult(BaseModel):
    recommended_scenario: str                # 추천 시나리오 유형 (확정 아님, 제안)
    reason: str                              # 현재 맥락에서 이 안을 추천하는 근거
    comparisons: list[ScenarioComparison] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)   # 승인 카드에 노출할 트레이드오프
    confidence: float = 0.7
