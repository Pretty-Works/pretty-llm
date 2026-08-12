# app/tests/test_replan_operation_required_fields.py
"""BE 재계획 API 최종 명세(§3 "필수 필드" 표)를 app/schemas/replan.py 가
모델 레벨에서 강제하는지 확인한다.

★ 배경
  from_/to/milestoneId/taskId/memberId/content/expectedContent 가 전부
  Optional 이라, apply_builder.py 가 before/after 에서 값을 못 찾아도(예:
  워커가 "before" 를 안 채운 경우) None 인 채로 조용히 통과해 BE 로 나갈 수
  있었다 — 특히 `from`은 BE 팀 체크리스트가 "가장 자주 나는 에러"라고 꼽은
  지점이다. app/schemas/replan.py 에 model_validator 를 추가해 operation
  종류별 필수 필드(§3 표)를 생성 시점에 강제하도록 고쳤다 — 이 테스트가 그걸
  확인한다.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.engine_b.apply_builder import build_operations
from app.schemas.replan import ReplanApplyRequest, ReplanOperation, ReplanSaveRequest, ReplanScenario
from app.schemas.state import ProposedChange, SynthesisResult


# ─── §3 표 그대로 — operation 별 필수 필드가 없으면 생성이 거부되는지 ──────

@pytest.mark.parametrize("kwargs,missing_field_in_message", [
    (dict(operation="PROJECT_TARGET_DATE_CHANGE", to="2026-08-25"), "from"),
    (dict(operation="PROJECT_TARGET_DATE_CHANGE", **{"from": "2026-08-22"}), "to"),
    (dict(operation="PROJECT_MEMBER_ADD"), "memberId"),
    (dict(operation="MILESTONE_TARGET_DATE_CHANGE", **{"from": "2026-08-20"}, to="2026-08-23"), "milestoneId"),
    (dict(operation="MILESTONE_TARGET_DATE_CHANGE", milestoneId=3, to="2026-08-23"), "from"),
    (dict(operation="TASK_DUE_DATE_CHANGE", **{"from": "2026-08-20"}, to="2026-08-23"), "taskId"),
    (dict(operation="TASK_CREATE", to="2026-08-22"), "content"),
    (dict(operation="TASK_CREATE", content="API 명세 검토"), "to"),
    (dict(operation="TASK_DELETE", taskId=101), "expectedContent"),
    (dict(operation="TASK_DELETE", expectedContent="API 명세 검토"), "taskId"),
])
def test_필수_필드가_없으면_생성이_거부된다(kwargs, missing_field_in_message):
    with pytest.raises(ValidationError) as exc:
        ReplanOperation(**kwargs)
    assert missing_field_in_message in str(exc.value)


def test_필수_필드가_다_있으면_정상_생성된다():
    """§3 표의 정상 예시 6종 — 전부 문제없이 만들어져야 한다."""
    ops = [
        ReplanOperation(operation="PROJECT_TARGET_DATE_CHANGE",
                        **{"from": "2026-08-22"}, to="2026-08-25"),
        ReplanOperation(operation="PROJECT_MEMBER_ADD", memberId=15, role="개발"),
        ReplanOperation(operation="MILESTONE_TARGET_DATE_CHANGE", milestoneId=3,
                        **{"from": "2026-08-20"}, to="2026-08-23"),
        ReplanOperation(operation="TASK_DUE_DATE_CHANGE", taskId=101,
                        **{"from": "2026-08-20"}, to="2026-08-23"),
        ReplanOperation(operation="TASK_CREATE", content="API 명세 검토",
                        to="2026-08-22", toAssigneeId=15),
        ReplanOperation(operation="TASK_DELETE", taskId=101, expectedContent="API 명세 검토"),
    ]
    assert len(ops) == 6
    # role/toAssigneeId 는 선택 필드라 없어도 통과해야 한다
    ReplanOperation(operation="PROJECT_MEMBER_ADD", memberId=16)
    ReplanOperation(operation="TASK_CREATE", content="문서화", to="2026-08-22")


def test_from_직렬화는_여전히_alias를_쓴다():
    """model_validator 를 추가해도 populate_by_name/alias 직렬화는 그대로여야
    한다 — BE 는 파이썬 예약어를 피한 from_ 이 아니라 from 을 기대한다."""
    op = ReplanOperation(operation="PROJECT_TARGET_DATE_CHANGE",
                         **{"from": "2026-08-22"}, to="2026-08-25")
    dumped = op.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dumped["from"] == "2026-08-22"
    assert "from_" not in dumped


# ─── apply_builder 통합 — before 가 비어 있으면 조정안 전체가 보류되는지 ──

def test_before가_비어있으면_apply_builder가_조정안_전체를_보류한다():
    """워커가 before 를 못 채운 조정안 — TASK_DUE_DATE_CHANGE 에 쓸 from 이
    없다. 예전엔 from_=None 인 채로 통과해 BE 로 나갔을 상황 — 지금은
    ReplanOperation 생성 시점에 ValidationError 로 걸려 build_operations() 가
    이 조정안 전체를 rejected 로 보류해야 한다(부분 적용 없음 원칙)."""
    result = SynthesisResult(
        scenario_id="EXTEND",
        proposed_changes=[
            ProposedChange(kind="task_due_date_change", target="task:101",
                           before={}, after={"due_date": "2026-08-23"}),
        ],
    )
    built = build_operations(result)

    assert not built.ok
    assert built.operations == []
    assert built.rejected
    assert "from" in built.rejected[0]["reason"] or "필수" in built.rejected[0]["reason"]


def test_before가_채워져있으면_정상_변환된다():
    result = SynthesisResult(
        scenario_id="EXTEND",
        proposed_changes=[
            ProposedChange(kind="task_due_date_change", target="task:101",
                           before={"due_date": "2026-08-20"}, after={"due_date": "2026-08-23"}),
        ],
    )
    built = build_operations(result)

    assert built.ok
    assert built.operations[0].operation == "TASK_DUE_DATE_CHANGE"
    assert built.operations[0].from_ == "2026-08-20"
    assert built.operations[0].to == "2026-08-23"


# ─── 상위 스키마(ReplanScenario/ReplanSaveRequest/ReplanApplyRequest) 회귀 ──

def test_replan_save_request는_시나리오_최대_5개():
    scenario = ReplanScenario(
        scenarioType="EXTEND", summary="테스트", risk="LOW",
        operations=[ReplanOperation(operation="PROJECT_MEMBER_ADD", memberId=1)],
    )
    with pytest.raises(ValidationError):
        ReplanSaveRequest(projectId=1, reason="사유", scenarios=[scenario] * 6)

    # 5개까진 통과해야 한다
    ReplanSaveRequest(projectId=1, reason="사유", scenarios=[scenario] * 5)


def test_replan_apply_request는_operations를_안_받는다():
    """반영 요청 스키마엔 operations 필드가 아예 없어야 한다 — BE 가 저장분에서
    꺼내 쓰므로 재전송하면 안 된다는 게 이 문서 §4-2 의 핵심 원칙이다."""
    assert "operations" not in ReplanApplyRequest.model_fields
    req = ReplanApplyRequest(projectId=1, replanId=123, scenarioType="EXTEND")
    assert req.model_dump(mode="json") == {
        "projectId": 1, "replanId": 123, "scenarioType": "EXTEND",
    }
