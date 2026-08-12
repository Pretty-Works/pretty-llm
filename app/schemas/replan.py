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

★ 2026-08-12 — operation 별 필수 필드(BE 명세 §3 표)를 모델 레벨에서 강제한다.
  이전엔 from_/to/milestoneId/taskId/memberId/content/expectedContent 가 전부
  Optional 이라, apply_builder.py 가 before/after 에서 값을 못 찾아도(예: LLM이
  proposed_changes.before 를 안 채운 경우) None 인 채로 조용히 통과해 BE 로
  나갔다 — BE 는 그 자리에서 REPLAN_004/005 로 뒤늦게 거부하지만, 그때는 이미
  승인 카드까지 띄운 뒤라 사용자 경험이 나쁘다. 특히 `from`은 BE 팀 체크리스트가
  "붙이는 동안 가장 자주 나는 에러"라고 꼽은 지점이라, 여기서(단일 출처인 Pydantic
  모델에서) 미리 막아 apply_builder.build_operations()의 기존 rejected 경로로
  조기에 걸러지게 한다(그 조정안은 build_operations 가 이미 하던 대로 통째로
  보류되고, propose_replan_scenarios 결과 텍스트에도 안 실린다).

Pydantic v2.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

# operation → 필수 필드 (BE 명세 §3 "필수 필드" 열 그대로, 단일 출처).
# 필드 이름은 파이썬 속성명 기준(from 은 alias 라 from_ 로 적는다).
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "PROJECT_TARGET_DATE_CHANGE": ("from_", "to"),
    "PROJECT_MEMBER_ADD": ("memberId",),
    "MILESTONE_TARGET_DATE_CHANGE": ("milestoneId", "from_", "to"),
    "TASK_DUE_DATE_CHANGE": ("taskId", "from_", "to"),
    "TASK_CREATE": ("content", "to"),
    "TASK_DELETE": ("taskId", "expectedContent"),
}


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

    ★ 위 표의 "필수 필드"는 아래 model_validator 가 강제한다 — 누락되면 이
      ReplanOperation 생성 자체가 ValidationError 로 실패하고, apply_builder.py의
      build_operations() 가 이미 그 예외를 잡아 해당 조정안 전체를 rejected 로
      보류한다(부분 적용 없음, 이 파일의 기존 원칙과 동일).
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

    @model_validator(mode="after")
    def _check_required_fields(self) -> "ReplanOperation":
        missing = [
            f for f in _REQUIRED_FIELDS.get(self.operation, ())
            if getattr(self, f) in (None, "")
        ]
        if missing:
            names = [("from" if f == "from_" else f) for f in missing]
            raise ValueError(
                f"{self.operation} 에 필수 필드 누락: {names} — BE 명세 §3 표를 "
                "참고해 채워야 한다(특히 from 은 계획 당시 조회한 현재 값을 그대로 "
                "담아야 충돌 검증이 된다)"
            )
        return self


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
