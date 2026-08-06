# app/workers/project/risk.py
"""project / risk 워커 — 목표 달성을 막을 구체적 위험."""

from typing import Literal

from pydantic import Field

from app.prompts import risk as prompt
from app.schemas.lenient import LenientModel
from app.tools.budget_tool import get_project_budget
from app.tools.hr_tool import list_user_leaves
from app.tools.project_query import PROJECT_TOOLS
from app.workers.base import WorkerSpec

RiskCategory = Literal["schedule", "resource", "scope", "quality", "cost", "external"]


class RiskItem(LenientModel):
    category: RiskCategory = "schedule"
    title: str = Field(default="", description="위험을 한 줄로")
    subject: str = Field(
        default="", description="지목 대상. 'todo:101', 'user:2' 처럼 식별자로"
    )
    likelihood: int = Field(default=50, description="발생 가능성 0~100")
    impact: int = Field(default=50, description="발생 시 타격 0~100")
    risk_score: int = Field(default=25, description="round(likelihood * impact / 100)")
    evidence_note: str = Field(
        default="", description="어떤 데이터를 보고 이렇게 판단했는지"
    )
    mitigation: str = Field(default="", description="실행 가능한 완화책")


class RiskResult(LenientModel):
    overall_risk_score: int = Field(
        default=0, description="0~100. 개별 위험의 평균이 아니라 종합 판단"
    )
    risks: list[RiskItem] = Field(default_factory=list)
    early_warnings: list[str] = Field(
        default_factory=list,
        description="아직 위험은 아니지만 지켜봐야 할 신호",
    )


SPEC = WorkerSpec(
    domain="project",
    dimension="risk",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=RiskResult,
    tools=(*PROJECT_TOOLS, get_project_budget, list_user_leaves),
    context_sections=("project", "todos", "members", "budget", "leaves", "workload"),
)
