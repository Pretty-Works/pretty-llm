"""
재계획 변경 종류(kind) 화이트리스트 — 단일 출처 (담당자3)

plan.apply(batch 적용)가 받는 changes[] 의 각 항목은 반드시 이 목록의 kind 여야 한다.
  · 우리(LLM)가 뱉는 proposed_changes → 이 kind로만 변환/검증한다.
  · BE는 kind마다 Service 핸들러를 붙인다.  (⚠ = 지금 개별 도구에 없어 신규 구현)

★ BE와 필드명 최종 합의 후 '여기만' 고치면 파이프라인 전체가 따라온다.
  (synthesis/tradeoff 프롬프트를 직접 손대지 않고, 이 검증 레이어로 kind 드리프트를 잡는다)
"""
from __future__ import annotations

# kind → 필수 필드 (식별자 위주. 나머지는 선택)
CHANGE_KINDS: dict[str, list[str]] = {
    "task.reassign":   ["taskId", "assigneeId"],            # ⚠ 신규 (task.update 제외)
    "task.updateDue":  ["taskId", "dueDate"],               # ⚠ 신규
    "task.create":     ["projectId", "content", "dueDate"], # ✅ 기존 task.create
    "task.drop":       ["taskId"],                          # ⚠ 신규 (또는 toggleStatus 재사용)
    "milestone.shift": ["milestoneId", "dueDate"],          # ⚠ 신규 (toggleStatus는 상태뿐)
    "schedule.create": ["title", "start", "end"],           # ✅ 기존 schedule.create
    "schedule.update": ["scheduleId"],                      # ✅ 기존 (start/end 선택)
    "budget.adjust":   ["projectId", "amount"],             # ⚠ 신규/확인
    "project.update":  ["projectId"],                       # ⚠ 신규 (endDate/scope 선택)
}


def is_valid_kind(kind: str) -> bool:
    return kind in CHANGE_KINDS


def validate_change(change: dict) -> tuple[bool, str]:
    """change 1건이 화이트리스트에 맞는지 검사. 반환: (ok, 사유)."""
    kind = change.get("kind")
    if kind not in CHANGE_KINDS:
        return False, f"알 수 없는 kind: {kind!r}"
    missing = [f for f in CHANGE_KINDS[kind] if change.get(f) in (None, "")]
    if missing:
        return False, f"{kind} 필수 필드 누락: {missing}"
    return True, ""


def partition_changes(changes: list[dict]) -> tuple[list[dict], list[dict]]:
    """유효/무효로 가른다.

    유효: 그대로 plan.apply changes[] 로.
    무효: {"change": ..., "reason": ...} 로 담아 로그/재생성에 쓴다.
    (LLM이 whitelist 밖 kind나 필드를 뱉어도 BE로 안 넘어가게 여기서 거른다)
    """
    valid: list[dict] = []
    rejected: list[dict] = []
    for c in changes:
        ok, reason = validate_change(c)
        if ok:
            valid.append(c)
        else:
            rejected.append({"change": c, "reason": reason})
    return valid, rejected
