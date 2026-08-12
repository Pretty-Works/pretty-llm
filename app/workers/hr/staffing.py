# app/workers/hr/staffing.py
"""hcm / staffing 워커 — 어느 기간이 빠듯하고, 넘긴다면 누가 받을 수 있나.

★ 2026-08-11 workload + skill_fit 합병.

  둘로 나뉘어 있을 이유가 없었다. workload 는 부하가 몰린 일을 넘길 후보를 내면서도
  "그 사람이 그 일을 할 수 있는지는 적합도 축의 판단"이라며 근거 없이 이름만 던졌고,
  skill_fit 은 점수를 걷어낸 뒤로는 역할 조회에 가까워 축이라기엔 얇았다.

  합치면 재배분 후보에 역할 근거가 붙는다 — 이게 합병의 실익이다.
    전: workload "B에게 넘길 수 있음" / skill_fit "B는 FE 역할" (따로)
    후: "이 일은 FE 성격이고 이 프로젝트 FE 는 B다. 그래서 B가 받을 수 있다"

  가용성 지표는 여전히 Context Builder 가 코드로 계산해 넘긴다.
  이 워커가 하는 일은 '그래서 그 기간이 감당 가능한가, 넘긴다면 누구에게'다.
"""

from pydantic import Field

from app.prompts import staffing as prompt
from app.schemas.lenient import LenientModel
from app.tools.hr_tool import find_user, list_member_leaves
from app.tools.project_query import list_project_members, list_project_tasks
from app.workers.base import WorkerSpec


class MemberLoad(LenientModel):
    """참여자별 집계. 상태 라벨은 없다 — 숫자를 그대로 인용하고 해석은 기간 단위로 한다."""

    user_id: int = 0
    name: str = ""
    open_todo_count: int = 0
    overdue_count: int = 0
    due_in_window_count: int = 0
    approved_leave_days: int = 0
    available_days: int = 0
    load_index: float | None = Field(
        default=None, description="컨텍스트에 주어진 값을 그대로 쓴다"
    )


class CrunchPeriod(LenientModel):
    """마감이 몰린 기간. 이 축의 1차 산출물이다."""

    period: str = Field(default="", description="'2026-08-17 ~ 2026-08-21' 또는 '8월 3주차'")
    due_task_ids: list[int] = Field(default_factory=list, description="그 기간에 마감인 할 일")
    absent_user_ids: list[int] = Field(
        default_factory=list, description="그 기간에 승인된 휴가로 자리에 없는 참여자"
    )
    assessment: str = Field(default="", description="이 기간이 빠듯한 이유. 사람 평가는 하지 말 것")


class RoleMatch(LenientModel):
    """넘겨받을 수 있는 참여자. 순위도 점수도 없다 — 근거만 적고 고르는 건 사용자다."""

    user_id: int = 0
    name: str = ""
    role: str = Field(default="", description="이 프로젝트에서 맡은 역할 (PM/BE/FE/QA/...)")
    basis: str = Field(
        default="",
        description="이 프로젝트에서의 역할과 처리한 할 일. 그 밖의 근거는 쓰지 말 것",
    )
    note: str = Field(default="", description="승인된 휴가로 인한 부재 등 참고사항")


class Handoff(LenientModel):
    """넘길 수 있는 일 1건. 후보에 역할 근거가 붙는다 — 합병의 실익이 여기다."""

    task_id: int = 0
    from_user_id: int = 0
    work_type: str = Field(
        default="", description="작업 성격: BE / FE / INFRA / QA / DESIGN / 기획"
    )
    candidates: list[RoleMatch] = Field(
        default_factory=list, description="그 역할을 맡은 참여자. 순위를 매기지 말 것"
    )
    reason: str = Field(default="", description="왜 넘겨야 하는지. 기간·마감으로 설명")


class StaffingResult(LenientModel):
    members: list[MemberLoad] = Field(default_factory=list)
    crunch_periods: list[CrunchPeriod] = Field(default_factory=list)
    handoffs: list[Handoff] = Field(default_factory=list)
    leave_gaps: list[str] = Field(
        default_factory=list,
        description="승인된 휴가로 생기는 공백 중 그 기간에 마감이 걸린 건",
    )
    unresolved: list[str] = Field(
        default_factory=list, description="역할을 특정하지 못했거나 맡은 사람이 없는 대상"
    )


SPEC = WorkerSpec(
    domain="hcm",
    dimension="staffing",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=StaffingResult,
    # 프로젝트 범위 도구만 준다. 전사 명부·타인 이력 조회 도구는 제거됐다.
    tools=(list_member_leaves, find_user, list_project_members, list_project_tasks),
    context_sections=(
        "project", "milestones", "todos", "members", "meetings",
        "workload", "leaves", "candidates",
    ),
)
