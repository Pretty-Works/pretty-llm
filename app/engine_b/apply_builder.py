# app/engine_b/apply_builder.py
"""
Apply Builder — 조정안(SynthesisResult) → ReplanOperation[] (담당자3)

proposed_changes(내부: kind/target/before/after) 를 BE 저장용 operations[] 로 바꾼다.
  · target 종류로 operation 을 고른다: todo/task → TASK_*, milestone → MILESTONE_*,
    project → PROJECT_*, member → PROJECT_MEMBER_ADD
  · after 로 to*(바꿀 값), before 로 from*(원래 값 — 충돌검증용) 을 채운다
  · 하나라도 검증 실패면 그 조정안 전체 보류(부분 적용 없음)

검증은 schemas/replan.py Pydantic 모델로 한다(단일 출처). DB 는 안 건드린다.

★ 2026-08-09 BE 스펙 전면 개정 — 표에 없는 건 애초에 못 만든다:
  · 담당자 재배정 없음 → synthesis 가 TASK_DELETE + TASK_CREATE 두 건으로 나눠 내야 한다
    (여기서 하나를 둘로 쪼개주지 않는다 — 새 담당자에게 갈 할 일 내용을 이 함수가 지어낼
    수 없기 때문에, "재배정 의도"를 판단하는 건 synthesis LLM 의 몫으로 남긴다).
  · 프로젝트 예산 변경 없음, 마일스톤 추가/삭제 없음, 참여자 제외(내보내기) 없음.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.schemas.replan import ReplanOperation
from app.schemas.state import ProposedChange, SynthesisResult


class _Unsupported(Exception):
    pass


@dataclass
class ApplyBuildResult:
    operations: list[ReplanOperation]
    rejected: list[dict]

    @property
    def ok(self) -> bool:
        return bool(self.operations) and not self.rejected


def build_operations(result: SynthesisResult) -> ApplyBuildResult:
    """조정안 1개의 proposed_changes 를 ReplanOperation 리스트로 바꾼다."""
    operations: list[ReplanOperation] = []
    rejected: list[dict] = []

    for pc in result.proposed_changes:
        try:
            operations.extend(_dispatch(pc))
        except ValidationError as exc:
            rejected.append({"change": pc.model_dump(), "reason": _first_error(exc)})
        except _Unsupported as exc:
            rejected.append({"change": pc.model_dump(), "reason": str(exc)})

    if rejected or not operations:
        return ApplyBuildResult(operations=[], rejected=rejected)
    return ApplyBuildResult(operations=operations, rejected=[])


# ─── ProposedChange 1건 → ReplanOperation 0~n건 ──────────────────

def _dispatch(pc: ProposedChange) -> list[ReplanOperation]:
    typ, ident = _parse_target(pc.target)
    kind = (pc.kind or "").lower()
    before, after = pc.before or {}, pc.after or {}
    if not typ:
        typ = kind.replace("_", ".").split(".")[0]

    if typ in ("todo", "task"):
        return _task(ident, kind, before, after)
    if typ == "milestone":
        return [_milestone(ident, before, after)]
    if typ == "project":
        return [_project(before, after)]
    if typ in ("member", "user"):
        return [_member(ident, kind, after)]
    raise _Unsupported(f"지원하지 않는 target/kind: {pc.target!r} / {pc.kind!r}")


def _task(ident: str, kind: str, before: dict, after: dict) -> list[ReplanOperation]:
    if any(w in kind for w in ("drop", "delete", "remove")):
        content = _pick(before, "content", "title") or _pick(after, "content", "title")
        if not content:
            raise _Unsupported(
                "TASK_DELETE 는 expectedContent(할 일 내용) 없이 못 만든다 — "
                "before 에 content/title 을 채워야 한다"
            )
        return [ReplanOperation(operation="TASK_DELETE", taskId=_as_int(ident),
                                expectedContent=content)]

    if any(w in kind for w in ("create", "add")) and not ident:
        content = _pick(after, "content", "title")
        to_due = _pick(after, "due_date", "dueDate")
        if not content or not to_due:
            raise _Unsupported(
                f"TASK_CREATE 는 content·to(마감일) 가 둘 다 있어야 한다: after={after}"
            )
        to_assignee = _pick(after, "assignee_id", "assigneeId")
        return [ReplanOperation(operation="TASK_CREATE", content=content, to=to_due,
                                toAssigneeId=_as_int(to_assignee) if to_assignee else None)]

    to_due = _pick(after, "due_date", "dueDate")
    if to_due is not None:
        return [ReplanOperation(operation="TASK_DUE_DATE_CHANGE", taskId=_as_int(ident),
                                from_=_pick(before, "due_date", "dueDate"), to=to_due)]

    if _pick(after, "assignee_id", "assigneeId") is not None:
        raise _Unsupported(
            "담당자 재배정 operation 은 없다 — TASK_DELETE(기존) + TASK_CREATE(신규, "
            "toAssigneeId=새 담당자) 두 건으로 나눠 만들어야 한다"
        )
    raise _Unsupported(f"task 변경 내용을 못 읽음: after={after}")


def _milestone(ident: str, before: dict, after: dict) -> ReplanOperation:
    to_due = _pick(after, "target_date", "targetDate", "due_date", "dueDate")
    if to_due is None:
        raise _Unsupported(f"milestone 변경 내용을 못 읽음: after={after}")
    return ReplanOperation(operation="MILESTONE_TARGET_DATE_CHANGE", milestoneId=_as_int(ident),
                           from_=_pick(before, "target_date", "targetDate", "due_date", "dueDate"),
                           to=to_due)


def _project(before: dict, after: dict) -> ReplanOperation:
    to_due = _pick(after, "target_date", "targetDate")
    if to_due is None:
        if _pick(after, "due_date", "dueDate") is not None:
            raise _Unsupported(
                "프로젝트 마감 변경은 키 이름이 target_date 여야 한다(due_date 아님)"
            )
        if _pick(after, "target_budget", "targetBudget", "budget", "amount") is not None:
            raise _Unsupported("프로젝트 예산 변경 operation 은 없다 — 지원하지 않는다")
        raise _Unsupported(f"project 변경 내용을 못 읽음: after={after}")
    return ReplanOperation(operation="PROJECT_TARGET_DATE_CHANGE",
                           from_=_pick(before, "target_date", "targetDate"), to=to_due)


def _member(ident: str, kind: str, after: dict) -> ReplanOperation:
    if any(w in kind for w in ("remove", "drop", "delete")):
        raise _Unsupported("참여자 제외(내보내기) operation 은 없다 — 지원하지 않는다")
    mid = ident or _pick(after, "member_id", "memberId", "user_id", "userId")
    if not mid:
        raise _Unsupported(f"member 대상을 특정 못 함: after={after}")
    return ReplanOperation(operation="PROJECT_MEMBER_ADD", memberId=_as_int(mid),
                           role=_pick(after, "role"))


# ─── 잡동사니 ─────────────────────────────────────────────────────

def _parse_target(target: str) -> tuple[str, str]:
    typ, sep, ident = (target or "").partition(":")
    if not sep:
        return "", (target or "").strip()
    return typ.strip().lower(), ident.strip()


def _pick(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _as_int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _first_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return "검증 실패"
    e = errs[0]
    loc = ".".join(str(x) for x in e.get("loc", ()))
    return f"{loc or '?'}: {e.get('msg', '검증 실패')}"
