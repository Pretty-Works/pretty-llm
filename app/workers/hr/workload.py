# app/workers/hr/workload.py
"""hcm / workload 워커 — 어느 기간이 빠듯하고, 언제 누가 자리에 없는가.

★ 2026-08-11 재설계. 주어를 사람에서 기간으로 옮겼다.
  예전에는 사람마다 OVERLOADED / TIGHT 같은 상태 라벨을 붙였다. 그건 개인 평가에
  가깝고, 근거가 되는 할 일 집계도 프로젝트 범위 밖은 볼 수 없어 반쪽이었다.

  이제는 "이민주가 과부하"가 아니라 "8월 3주차에 마감 3건이 몰려 있고 담당자 2명이 부재"
  라고 답한다. 같은 데이터로 더 정확하고, 사람을 평가하지 않는다.

셀 수 있는 지표는 Context Builder 가 코드로 계산해서 넘긴다.
이 워커가 하는 일은 '그래서 그 기간이 감당 가능한가'를 판단하는 것이다.
"""

from pydantic import Field

from app.prompts import workload as prompt
from app.schemas.lenient import LenientModel
from app.tools.hr_tool import find_user, list_member_leaves
from app.tools.project_query import list_project_tasks
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
    """마감이 몰린 기간. 이 축의 핵심 산출물이다."""

    period: str = Field(default="", description="'2026-08-17 ~ 2026-08-21' 또는 '8월 3주차'")
    due_task_ids: list[int] = Field(default_factory=list, description="그 기간에 마감인 할 일")
    absent_user_ids: list[int] = Field(
        default_factory=list, description="그 기간에 승인된 휴가로 자리에 없는 참여자"
    )
    assessment: str = Field(default="", description="이 기간이 빠듯한 이유. 사람 평가는 하지 말 것")


class RebalanceHint(LenientModel):
    """이름이 아니라 user_id 로 받는다 — 모델이 이름을 넣으면 검증에서 UNKNOWN_USER 로 걸린다."""

    task_id: int = 0
    from_user_id: int = 0
    to_user_candidates: list[int] = Field(
        default_factory=list, description="넘길 수 있는 후보의 user_id(숫자). 확정이 아니라 후보"
    )
    reason: str = ""


class WorkloadResult(LenientModel):
    members: list[MemberLoad] = Field(default_factory=list)
    crunch_periods: list[CrunchPeriod] = Field(default_factory=list)
    rebalance_hints: list[RebalanceHint] = Field(default_factory=list)
    leave_gaps: list[str] = Field(
        default_factory=list,
        description="승인된 휴가로 생기는 공백 중 그 기간에 마감이 걸린 건",
    )


SPEC = WorkerSpec(
    domain="hcm",
    dimension="workload",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=WorkloadResult,
    tools=(list_member_leaves, find_user, list_project_tasks),
    context_sections=("project", "todos", "members", "workload", "leaves"),
)
