# app/engine_b/apply_builder.py
"""
Apply Builder — 조정안(SynthesisResult) → ApplyRequest (도메인별 묶음) (담당자3)

proposed_changes(내부: kind/target/before/after) 를 BE 저장/반영용 applyRequest 로 바꾼다.
  · target 종류로 도메인 버킷을 고른다: todo/task → task, milestone, project, member/user
  · after 로 to*(바꿀 값), before 로 from*(원래 값 — 충돌검증용) 을 채운다
  · 하나라도 검증 실패면 그 조정안 전체 보류(부분 적용 없음)

검증은 schemas/replan.py Pydantic 모델로 한다(단일 출처). DB 는 안 건드린다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.schemas.replan import (
    ApplyRequest, MemberChange, MilestoneChange, ProjectChange, TaskChange,
)
from app.schemas.state import ProposedChange, SynthesisResult


class _Unsupported(Exception):
    pass


@dataclass
class ApplyBuildResult:
    apply_request: ApplyRequest | None
    rejected: list[dict]

    @property
    def ok(self) -> bool:
        return self.apply_request is not None


def build_apply_request(result: SynthesisResult) -> ApplyBuildResult:
    """조정안 1개의 proposed_changes 를 도메인별 ApplyRequest 로 묶는다."""
    members: list[MemberChange] = []
    tasks: list[TaskChange] = []
    milestones: list[MilestoneChange] = []
    projects: list[ProjectChange] = []
    rejected: list[dict] = []

    for pc in result.proposed_changes:
        try:
            _dispatch(pc, members, tasks, milestones, projects)
        except ValidationError as exc:
            rejected.append({"change": pc.model_dump(), "reason": _first_error(exc)})
        except _Unsupported as exc:
            rejected.append({"change": pc.model_dump(), "reason": str(exc)})

    apply_request = ApplyRequest(
        memberChanges=members, taskChanges=tasks,
        milestoneChanges=milestones, projectChanges=projects,
    )
    if rejected or apply_request.is_empty():
        return ApplyBuildResult(apply_request=None, rejected=rejected)
    return ApplyBuildResult(apply_request=apply_request, rejected=[])


# ─── ProposedChange 1건 → 해당 버킷에 append ──────────────────────

def _dispatch(pc: ProposedChange, members, tasks, milestones, projects) -> None:
    typ, ident = _parse_target(pc.target)
    kind = (pc.kind or "").lower()
    before, after = pc.before or {}, pc.after or {}
    if not typ:
        typ = kind.replace("_", ".").split(".")[0]

    if typ in ("todo", "task"):
        tasks.extend(_task(ident, kind, before, after))
    elif typ == "milestone":
        milestones.append(MilestoneChange(
            milestoneId=ident,
            fromDueDate=_pick(before, "target_date", "targetDate", "due_date", "dueDate"),
            toDueDate=_pick(after, "target_date", "targetDate", "due_date", "dueDate"),
        ))
    elif typ == "project":
        projects.extend(_project(before, after))
    elif typ in ("member", "user"):
        members.append(_member(ident, kind, after))
    else:
        raise _Unsupported(f"지원하지 않는 target/kind: {pc.target!r} / {pc.kind!r}")


def _task(ident, kind, before, after) -> list[TaskChange]:
    if any(w in kind for w in ("drop", "delete", "remove")) or _truthy(_pick(after, "dropped", "deleted")):
        return [TaskChange(taskId=ident, action="REMOVE")]
    out: list[TaskChange] = []
    to_assignee = _pick(after, "assignee_id", "assigneeId")
    if to_assignee is not None:
        out.append(TaskChange(taskId=ident, action="REASSIGN",
                              fromAssigneeId=_pick(before, "assignee_id", "assigneeId"),
                              toAssigneeId=to_assignee))
    to_due = _pick(after, "due_date", "dueDate")
    if to_due is not None:
        out.append(TaskChange(taskId=ident, action="UPDATE_DUE",
                              fromDueDate=_pick(before, "due_date", "dueDate"),
                              toDueDate=to_due))
    if not out:
        raise _Unsupported(f"task 변경 내용을 못 읽음: after={after}")
    return out


def _project(before, after) -> list[ProjectChange]:
    out: list[ProjectChange] = []
    to_due = _pick(after, "target_date", "targetDate", "end_date", "endDate", "deadline")
    if to_due is not None:
        out.append(ProjectChange(
            field="deadline",
            from_=_pick(before, "target_date", "targetDate", "end_date", "endDate", "deadline"),
            to=to_due))
    to_budget = _pick(after, "target_budget", "targetBudget", "budget", "amount")
    if to_budget is not None:
        out.append(ProjectChange(
            field="budget",
            from_=_pick(before, "target_budget", "targetBudget", "budget", "amount"),
            to=to_budget))
    if not out:
        raise _Unsupported(f"project 변경 내용을 못 읽음: after={after}")
    return out


def _member(ident, kind, after) -> MemberChange:
    mid = ident or _pick(after, "member_id", "memberId", "user_id", "userId")
    remove = any(w in kind for w in ("remove", "drop", "delete")) or _truthy(_pick(after, "removed"))
    return MemberChange(memberId=mid, action="REMOVE_FROM_PROJECT" if remove else "ADD_TO_PROJECT")


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


def _truthy(v: Any) -> bool:
    return v in (True, "true", "True", 1, "1")


def _first_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return "검증 실패"
    e = errs[0]
    loc = ".".join(str(x) for x in e.get("loc", ()))
    return f"{loc or '?'}: {e.get('msg', '검증 실패')}"
