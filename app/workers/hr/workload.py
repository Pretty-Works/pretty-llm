# app/workers/hr/workload.py
"""hcm / workload 워커 — 누구에게 일이 몰려 있고 어디가 병목인가.

셀 수 있는 지표는 Context Builder 가 코드로 계산해서 넘긴다.
이 워커가 하는 일은 '그 숫자가 과부하를 뜻하는가'를 판단하는 것이다.
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.prompts import workload as prompt
from app.tools.hr_tool import find_user, get_user_workload, list_user_leaves
from app.tools.project_tool import list_project_tasks
from app.workers.base import WorkerSpec

LoadStatus = Literal["OVERLOADED", "TIGHT", "BALANCED", "AVAILABLE"]


class MemberLoad(BaseModel):
    user_id: int = 0
    name: str = ""
    status: LoadStatus = "BALANCED"
    open_todo_count: int = 0
    overdue_count: int = 0
    due_in_window_count: int = 0
    approved_leave_days: int = 0
    available_days: int = 0
    load_index: float | None = Field(
        default=None, description="컨텍스트에 주어진 값을 그대로 쓴다"
    )
    note: str = Field(default="", description="이 상태로 판단한 이유")


class RebalanceHint(BaseModel):
    task_id: int = 0
    from_user_id: str = ""
    to_user_candidates: list[str] = Field(
        default_factory=list, description="넘길 수 있는 후보 user_id. 확정이 아니라 후보"
    )
    reason: str = ""


class WorkloadResult(BaseModel):
    members: list[MemberLoad] = Field(default_factory=list)
    bottlenecks: list[str] = Field(
        default_factory=list,
        description="이 사람이 막히면 프로젝트가 멈추는 사람. 단순히 바쁜 사람이 아니다",
    )
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
    tools=(get_user_workload, list_user_leaves, find_user, list_project_tasks),
    context_sections=("project", "todos", "members", "workload", "leaves"),
)
