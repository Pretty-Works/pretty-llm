# app/workers/hr/skill_fit.py
"""hcm / skill_fit 워커 — 이 일이 어느 역할의 일이고, 그 역할은 누가 맡고 있는가.

★ 2026-08-11 재설계. 개인 점수 매기기를 뺐다.
  예전에는 후보마다 fit_score(0~100)를 매겼고, 그 점수가 replan 을 타면 실제
  업무 재배분 제안이 됐다. LLM 이 사람에게 점수를 매겨 배치를 바꾸는 구조였다.

  근거도 없다 — 프롬프트가 요구하던 4가지 중 입사일(BE 응답에 필드 없음)과
  타인의 과거 프로젝트 이력(BE 가 요청자 스코프로 막음)은 확보할 수 없다.

  그래서 축의 성격을 바꿨다: **점수 대신 역할 근거만 제시하고 선택은 사람이 한다.**
  근거는 프로젝트 안에서만 찾는다 — 참여자의 역할과 이 프로젝트에서 처리한 할 일.
  둘 다 프로젝트 화면에 이미 보이는 정보다.
"""

from typing import Literal

from pydantic import Field

from app.prompts import skill_fit as prompt
from app.schemas.lenient import LenientModel
from app.tools.hr_tool import find_user
from app.tools.project_query import list_project_members, list_project_tasks
from app.workers.base import WorkerSpec


class RoleMatch(LenientModel):
    """후보 1명. 순위도 점수도 없다 — 근거만 적고 고르는 건 사용자다."""

    user_id: int = 0
    name: str = ""
    role: str = Field(default="", description="이 프로젝트에서 맡은 역할 (PM/BE/FE/QA/...)")
    basis: str = Field(
        default="",
        description="이 프로젝트에서의 역할과 처리한 할 일. 그 밖의 근거는 쓰지 말 것",
    )
    note: str = Field(default="", description="승인된 휴가로 인한 부재 등 참고사항")


class Assignment(LenientModel):
    target: str = Field(default="", description="'todo:101' 또는 'project:1003'")
    target_kind: Literal["task", "project", "role"] = "task"
    work_type: str = Field(
        default="", description="작업 성격: BE / FE / INFRA / QA / DESIGN / 기획"
    )
    matches: list[RoleMatch] = Field(
        default_factory=list, description="해당 역할을 맡은 참여자. 순위를 매기지 말 것"
    )
    rationale: str = Field(default="", description="이 작업을 그 성격으로 본 이유")


class SkillFitResult(LenientModel):
    assignments: list[Assignment] = Field(default_factory=list)
    unresolved: list[str] = Field(
        default_factory=list, description="역할을 특정하지 못했거나 맡은 사람이 없는 대상"
    )


SPEC = WorkerSpec(
    domain="hcm",
    dimension="skill_fit",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=SkillFitResult,
    # 프로젝트 범위 도구만 준다. 전사 명부·타인 이력 조회 도구는 제거됐다.
    tools=(find_user, list_project_members, list_project_tasks),
    context_sections=("project", "todos", "members", "candidates", "leaves"),
)
