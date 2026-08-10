# app/schemas/replan.py
"""
Replan 저장/반영 계약 — BE 실제 구현(2026-08-09 스펙 전면 개정판)에 맞춘 버전.

흐름:
  생성  Agent → BE  POST /projects/{pid}/replans                     body: ReplanSaveRequest
        (★ 승인 필요 — 저장도 HITL 대상이다. 프로젝트 데이터는 안 바뀌지만, 에이전트가
        외부 텍스트를 읽고 만든 계획이 "공식 기록"이 되는 시점이라 사람이 한 번 본다.)
  반영  Agent → BE  POST /projects/{pid}/replans/{replanId}/apply    body: ReplanApplyRequest
        (BE 가 저장분에서 그 scenario 의 operations 를 꺼내 트랜잭션으로 반영. 승인 필요.)

★ 반영 때 operations 를 다시 안 보낸다 — BE 가 갖고 있다. Agent 는 어느 안인지(scenarioType)만.
★ 변경은 operations[] 배열 하나로 표현한다(ReplanOperationType 6종). 예전 memberChanges/
  taskChanges/milestoneChanges/projectChanges 4갈래 구조는 폐기됐다.
★ scenarioType 은 REALLOCATE(인력재배치) / EXTEND(일정조정) / REDUCE_SCOPE(범위축소) 3종,
  전부 대문자로 BE 와 맞춘다(예전엔 extend/add_resource/reduce_scope 소문자였다).
★ path 변수(projectId·replanId)는 본문에도 그대로 담아야 한다 — 승인 토큰이 본문 해시로
  봉인되므로, 경로만으로 대상을 정하면 승인 때와 다른 프로젝트로 보내도 해시가 그대로다.

Pydantic v2.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ScenarioType = Literal["REALLOCATE", "EXTEND", "REDUCE_SCOPE"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]

OperationType = Literal[
    "PROJECT_TARGET_DATE_CHANGE",
    "PROJECT_MEMBER_ADD",
    "MILESTONE_TARGET_DATE_CHANGE",
    "TASK_DUE_DATE_CHANGE",
    "TASK_CREATE",
    "TASK_DELETE",
]


class ReplanOperation(BaseModel):
    """변경 단위 하나. operation 종류에 따라 실제로 쓰는 필드가 다르다.

      PROJECT_TARGET_DATE_CHANGE   from, to
      PROJECT_MEMBER_ADD           memberId (role 선택)
      MILESTONE_TARGET_DATE_CHANGE milestoneId, from, to
      TASK_DUE_DATE_CHANGE         taskId, from, to
      TASK_CREATE                  content, to (toAssigneeId 선택)
      TASK_DELETE                  taskId, expectedContent

    ★ 표에 없는 건 못 한다: 담당자 재배정(TASK_DELETE+TASK_CREATE 조합으로 표현),
      프로젝트 시작일/예산/이름 변경, 마일스톤 추가/삭제, 참여자 제외 — 전부 불가능한
      operation 이라 아예 만들면 안 된다(app/engine_b/apply_builder.py 가 걸러낸다).
    """
    model_config = ConfigDict(populate_by_name=True)

    operation: OperationType
    # PROJECT_TARGET_DATE_CHANGE / MILESTONE_TARGET_DATE_CHANGE / TASK_DUE_DATE_CHANGE
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    # MILESTONE_TARGET_DATE_CHANGE / TASK_DUE_DATE_CHANGE / TASK_DELETE
    milestoneId: int | None = None
    taskId: int | None = None
    # PROJECT_MEMBER_ADD
    memberId: int | None = None
    role: str | None = None
    # TASK_CREATE
    content: str | None = None
    toAssigneeId: int | None = None
    # TASK_DELETE — 하드 삭제라 내용까지 대조한다(되돌릴 수 없어서)
    expectedContent: str | None = None


class ReplanScenario(BaseModel):
    scenarioType: ScenarioType
    summary: str = Field(min_length=1, max_length=500)
    risk: RiskLevel
    operations: list[ReplanOperation] = Field(min_length=1, max_length=50)


class ReplanSaveRequest(BaseModel):
    """저장(replan.create): 시나리오 최대 5개를 저장한다. ★ 승인 필요(HITL) — 실
    데이터 변경은 없지만, 회의록·게시글 같은 외부 텍스트를 읽고 만든 계획이 처음으로
    "공식 기록"이 되는 지점이라 사람이 한 번 확인한다."""
    projectId: int
    reason: str = Field(min_length=1, max_length=500)
    scenarios: list[ReplanScenario] = Field(min_length=1, max_length=5)


class ReplanApplyRequest(BaseModel):
    """반영(replan.apply): 어느 안인지만 보낸다. 실제 변경 내용(operations)은 다시
    안 보낸다 — BE 가 저장분에서 그대로 꺼내 쓴다(재승인 사이 조작 방지)."""
    projectId: int
    replanId: int
    scenarioType: ScenarioType
