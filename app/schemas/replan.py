# app/schemas/replan.py
"""
Replan 저장/반영 계약 — BE 가 저장하고 BE 가 반영한다 (담당자3)

흐름:
  생성  Agent → BE  POST /projects/{pid}/replans                     body: ReplanSaveRequest
        (실 데이터 변경 없음. '제안'만 저장. BE 가 replanId 발급)
  반영  Agent → BE  POST /projects/{pid}/replans/{replanId}/apply    body: ReplanApplyRequest
        (BE 가 저장분에서 그 scenario 의 applyRequest 를 꺼내 트랜잭션으로 반영)

★ 반영 때 applyRequest 를 다시 안 보낸다 — BE 가 갖고 있다. Agent 는 어느 안인지(scenarioType)만.
★ applyRequest 는 도메인별로 묶는다(BE 의 Service 조합에 그대로 대응):
    memberChanges / taskChanges / milestoneChanges / projectChanges
★ 충돌 검증 대비: 바꿀 값은 to*, 생성 시점의 원래 값은 from* 로 함께 담는다(있으면).
  BE 는 반영 직전 현재 DB 가 from* 과 같은지 확인해 그 사이 남이 바꿨으면 REPLAN_CONFLICT.

★ scenarioType 은 scenario_executor.SCENARIO_LABELS 키(extend/add_resource/reduce_scope)와 같다.
  (문서에 'reallocate' 로 적혔던 건 코드상 'add_resource'. 이름 바꾸려면 SCENARIO_LABELS 한 곳만.)

Pydantic v2.
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ScenarioType = Literal["extend", "add_resource", "reduce_scope"]


# ─── 도메인별 변경 단위 ───────────────────────────────────────────

class MemberChange(BaseModel):
    memberId: int
    action: Literal["ADD_TO_PROJECT", "REMOVE_FROM_PROJECT"]


class TaskChange(BaseModel):
    taskId: int
    action: Literal["REASSIGN", "UPDATE_DUE", "REMOVE"]
    fromAssigneeId: int | None = None
    toAssigneeId: int | None = None
    fromDueDate: date | None = None
    toDueDate: date | None = None


class MilestoneChange(BaseModel):
    milestoneId: int
    fromDueDate: date | None = None
    toDueDate: date


class ProjectChange(BaseModel):
    """프로젝트는 필드가 여럿이라 field/from/to 로 일반화."""
    model_config = ConfigDict(populate_by_name=True)
    field: Literal["deadline", "budget"]
    from_: str | int | None = Field(default=None, alias="from")
    to: str | int


# ─── applyRequest (도메인별 묶음) ─────────────────────────────────

class ApplyRequest(BaseModel):
    memberChanges: list[MemberChange] = Field(default_factory=list)
    taskChanges: list[TaskChange] = Field(default_factory=list)
    milestoneChanges: list[MilestoneChange] = Field(default_factory=list)
    projectChanges: list[ProjectChange] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.memberChanges or self.taskChanges
                    or self.milestoneChanges or self.projectChanges)


class Comparison(BaseModel):
    """3안 비교 표시용(저장만 — 실제 반영과 무관)."""
    summary: str = ""
    risk: str = ""              # 낮음/중간/높음 (또는 LOW/MEDIUM/HIGH)
    scheduleRecovery: str = ""
    cost: str = ""


class ReplanScenario(BaseModel):
    scenarioType: ScenarioType
    comparison: Comparison
    applyRequest: ApplyRequest


# ─── Agent → BE body ──────────────────────────────────────────────

class ReplanSaveRequest(BaseModel):
    """생성: 3안을 저장한다(실 데이터 변경 없음)."""
    scenarios: list[ReplanScenario] = Field(min_length=1)


class ReplanApplyRequest(BaseModel):
    """반영: 어느 안인지만. 배치는 BE 가 저장분에서 꺼낸다."""
    scenarioType: ScenarioType
