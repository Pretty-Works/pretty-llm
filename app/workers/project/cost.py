# app/workers/project/cost.py
"""project / cost 워커 — 예산 소진 속도와 초과 위험."""

from pydantic import Field

from app.prompts import cost as prompt
from app.schemas.lenient import LenientModel
from app.tools.budget_tool import BUDGET_TOOLS
from app.tools.project_query import get_project_overview
from app.workers.base import WorkerSpec


class CostDriver(LenientModel):
    category: str = Field(default="", description="외주 / 라이선스 / 인프라 등")
    amount: int = 0
    note: str = ""


class CostLever(LenientModel):
    action: str = Field(default="", description="절감 수단")
    expected_saving: int = Field(default=0, description="예상 절감액(원)")
    tradeoff: str = Field(
        default="", description="이걸 하면 무엇을 포기하게 되는지. 비워두지 말 것"
    )


class CostResult(LenientModel):
    budget_total: int = 0
    spent: int = 0
    committed: int = Field(default=0, description="결재 진행 중이라 사실상 묶인 금액")
    remaining: int = Field(default=0, description="total - spent - committed")
    usage_ratio: float = Field(default=0.0, description="(spent+committed)/total")
    elapsed_ratio: float = Field(
        default=0.0, description="(오늘-시작일)/(목표일-시작일)"
    )
    projected_total: int = Field(
        default=0, description="현 소진 속도 유지 시 목표일까지의 총 지출 추정"
    )
    overrun_amount: int = Field(default=0, description="초과 예상액. 초과 없으면 0")
    on_track: bool = Field(default=True, description="예산 안에서 끝날 궤도인가")
    drivers: list[CostDriver] = Field(default_factory=list)
    levers: list[CostLever] = Field(default_factory=list)


SPEC = WorkerSpec(
    domain="project",
    dimension="cost",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=CostResult,
    tools=(*BUDGET_TOOLS, get_project_overview),
    context_sections=("project", "milestones", "budget", "todos"),
)
