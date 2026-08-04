# app/workers/hr/skill_fit.py
"""hcm / skill_fit 워커 — 이 일에 누가 맞는가.

ERD 에 스킬 컬럼이 없어 적합도는 '조회'가 아니라 '추론'이다.
그래서 이 워커의 confidence 는 구조적으로 0.75 를 넘을 수 없다. (프롬프트에 명시)
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.prompts import skill_fit as prompt
from app.tools.hr_tool import (
    find_user,
    get_user_project_history,
    list_department_members,
    list_user_tasks,
)
from app.tools.project_query import list_project_members, list_project_tasks
from app.workers.base import WorkerSpec


class Candidate(BaseModel):
    user_id: int = 0
    name: str = ""
    fit_score: int = Field(default=50, description="0~100")
    basis: str = Field(
        default="",
        description="부서/직책/과거 role/처리한 할 일 중 무엇을 근거로 삼았는지 구체적으로",
    )
    external: bool = Field(
        default=False, description="이 프로젝트 참여자가 아니라 외부에서 투입해야 하는가"
    )
    note: str = Field(default="", description="승인된 휴가 등 참고사항")


class Assignment(BaseModel):
    target: str = Field(default="", description="'todo:101' 또는 'project:1003'")
    target_kind: Literal["task", "project", "role"] = "task"
    work_type: str = Field(
        default="", description="작업 성격: BE / FE / INFRA / QA / DESIGN / 기획"
    )
    recommended: Candidate
    alternatives: list[Candidate] = Field(
        default_factory=list, description="최소 1명. 1순위가 빠질 때의 대안"
    )
    rationale: str = ""


class SkillFitResult(BaseModel):
    assignments: list[Assignment] = Field(default_factory=list)
    inference_basis: str = Field(
        default="",
        description="스킬 데이터가 없어 무엇으로 대체 추론했는지 한 문단",
    )
    unresolved: list[str] = Field(
        default_factory=list, description="근거가 부족해 판단하지 못한 대상"
    )


SPEC = WorkerSpec(
    domain="hcm",
    dimension="skill_fit",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=SkillFitResult,
    tools=(
        find_user,
        list_department_members,
        get_user_project_history,
        list_user_tasks,
        list_project_members,
        list_project_tasks,
    ),
    context_sections=("project", "todos", "members", "candidates", "leaves"),
)
