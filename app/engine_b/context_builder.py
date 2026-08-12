# app/engine_b/context_builder.py
"""Context Builder — 워커들이 공통으로 볼 사실을 한 번에 모은다.

워커 5개가 각자 같은 프로젝트/할 일/인원을 다시 조회하면 토큰과 시간이 그대로 5배가 된다.
그래서 공통분모는 여기서 코드로 한 번만 모으고, 워커는 부족한 것만 툴로 채운다.

셀 수 있는 것(진행률, 부하 지표, 잔여 예산)은 여기서 코드로 계산한다.
LLM 에게는 '그래서 이게 문제인가'만 묻는다.
"""

from datetime import date, datetime, timedelta
from typing import Any, Iterable

from app.config import get_settings
from app.schemas.state import (
    AnalysisContext,
    AnalysisPlan,
    AnalysisRequest,
    BudgetSnapshot,
    LeaveSnapshot,
    MeetingSnapshot,
    MemberSnapshot,
    MilestoneSnapshot,
    MyWeekSnapshot,
    ProjectSnapshot,
    ScenarioSpec,
    TodoSnapshot,
)
from app.tools import budget_tool, hr_tool, project_query
from app.utils.logger import get_logger
from app.utils.parser import parse_date

log = get_logger("engine_b.context_builder")

# 기간이 특정되지 않았을 때 들여다볼 기본 창(일)
_DEFAULT_WINDOW_DAYS = 60


async def build_context(plan: AnalysisPlan, request: AnalysisRequest) -> AnalysisContext:
    """라우팅 결과를 바탕으로 분석에 필요한 사실을 모은다."""
    settings = get_settings()
    as_of = request.as_of or settings.as_of()

    context = AnalysisContext(as_of=as_of)
    # 요청자는 user.me 로만 잡는다. id 로 남의 프로필을 여는 경로를 두지 않기 위해서다.
    context.requester = _member_from_user(await hr_tool.fetch_requester())
    if context.requester is None:
        context.missing.append("요청자 정보 조회 실패")

    project_ids = await _resolve_project_ids(plan, request)
    if not project_ids:
        context.missing.append("대상 프로젝트를 특정하지 못함")

    for project_id in project_ids:
        snapshot = await _load_project(project_id)
        if snapshot is None:
            context.missing.append(f"프로젝트 조회 실패: {project_id}")
            continue
        context.projects.append(snapshot)

        budget = await _load_budget(project_id)
        if budget is None:
            context.missing.append(f"예산 정보 없음: {project_id}")
        else:
            context.budgets.append(budget)

    window_from, window_to = _resolve_window(plan, context, as_of)
    context.window_from, context.window_to = window_from, window_to

    # 사람 관련 도메인일 때만 인력 데이터를 채운다 (불필요한 조회 방지)
    if "hcm" in plan.domains or "vacation" in plan.domains:
        await _load_people(plan, context, window_from, window_to)

    if "me" in plan.domains:
        context.my_week = await _load_my_week(context, window_from, window_to)
        if context.my_week is None:
            context.missing.append("내 주간 정보 조회 실패")

    apply_data_gate(context)

    log.info(
        "context: 프로젝트 %d건, 후보 %d명, 마일스톤 %d건, 회의록 %d건, "
        "기간 %s~%s, 미확보 %d건, 스킵 %s",
        len(context.projects),
        len(context.candidates),
        sum(len(p.milestones) for p in context.projects),
        sum(len(p.meetings) for p in context.projects),
        window_from,
        window_to,
        len(context.missing),
        context.skipped or "없음",
    )
    return context


# ─── Data Gate — 근거 없는 축은 돌리지 않는다 ─────────────────────
#
# 예전에는 컨텍스트가 비면 "워커가 도구로 직접 찾아라"고 넘겼고, 그 경로가
# 전사 명부 조회로 이어져 존재하지 않는 사람이 분석에 섞였다. 근거가 없으면
# 워커를 돌리지 않고 못 봤다고 답하는 쪽이 맞다.

_DIMENSION_NEEDS: dict[str, tuple[str, str]] = {
    "priority": ("projects", "대상 프로젝트"),
    "risk": ("projects", "대상 프로젝트"),
    "cost": ("budgets", "예산 정보"),
    "staffing": ("candidates", "프로젝트 참여자"),
    "my_week": ("my_week", "내 주간 정보"),
}


def apply_data_gate(context: AnalysisContext) -> None:
    """근거가 없는 축을 `skipped` 에 기록한다. graph 가 이 목록을 보고 워커를 건너뛴다."""
    for dimension, (field, label) in _DIMENSION_NEEDS.items():
        if not getattr(context, field, None):
            context.skipped.append(f"{dimension}: {label}를 확보하지 못해 분석하지 않음")

    # followup 은 회의록이 근거의 전부다. 회의록은 프로젝트 안에 있어 위 반복으로 못 잡는다.
    if not any(p.meetings for p in context.projects):
        context.skipped.append("followup: 회의록을 확보하지 못해 분석하지 않음")

    if context.projects and not any(p.milestones for p in context.projects):
        context.missing.append(
            "마일스톤 없음 — 일정 판단 근거가 할 일뿐이다. 진척률을 단정하지 말 것"
        )


def skipped_dimensions(context: AnalysisContext) -> set[str]:
    """`skipped` 문자열에서 축 이름만 뽑는다."""
    return {item.split(":", 1)[0].strip() for item in context.skipped}


# ─── 대상 해석 ────────────────────────────────────────────────────

async def _resolve_project_ids(plan: AnalysisPlan, request: AnalysisRequest) -> list[str]:
    """id → 이름 → 화면 컨텍스트 → 참여 프로젝트 순으로 대상을 좁힌다."""
    ids: list[str] = list(plan.entities.project_ids)

    for name in plan.entities.project_names:
        project = await project_query.fetch_project(None, project_name=name)
        if project and project["id"] not in ids:
            ids.append(project["id"])

    if not ids and request.ui_context.project_id:
        ids.append(request.ui_context.project_id)

    if not ids:
        # 대상이 없으면 요청자가 참여 중인 진행 프로젝트를 본다.
        import json

        try:
            payload = json.loads(await project_query.find_projects.ainvoke({"user_id": request.user_id}))
            ids = [
                p["id"]
                for p in payload.get("projects", [])
                if p.get("status") in {"ACTIVE", "PLANNING"}
            ]
        except Exception as exc:
            log.warning("참여 프로젝트 조회 실패: %s", exc)

    return list(dict.fromkeys(ids))


def _resolve_window(
    plan: AnalysisPlan, context: AnalysisContext, as_of: date
) -> tuple[date, date]:
    """분석 대상 기간. 질문에 기간이 있으면 그것, 없으면 목표일까지."""
    start = plan.entities.date_from or as_of
    if plan.entities.date_to:
        return start, plan.entities.date_to

    due_dates = [p.due_date for p in context.projects if p.due_date]
    if due_dates:
        return start, max(max(due_dates), start)
    return start, start + timedelta(days=_DEFAULT_WINDOW_DAYS)


# ─── 적재 ─────────────────────────────────────────────────────────

async def _load_project(project_id: int) -> ProjectSnapshot | None:
    raw = await project_query.fetch_project(project_id)
    if not raw:
        return None

    members = [
        MemberSnapshot(
            user_id=m["id"],
            name=m.get("name", ""),
            department=m.get("department"),
            position=m.get("position"),
            role=m.get("role"),
            status=m.get("status"),
        )
        for m in await project_query._members(project_id)
    ]

    todos = [
        TodoSnapshot(
            id=t["id"],
            project_id=t.get("project_id", project_id),
            title=t.get("title", ""),
            status=t.get("status", "TODO"),
            due_date=parse_date(t.get("due_date")),
            assignee_id=t.get("assignee_id"),
            assignee_name=t.get("assignee_name"),
        )
        for t in await project_query._todos(project_id)
    ]

    # 마일스톤은 기간 제약 없이 전체를 받는 유일한 일정 근거다 (할 일은 주 단위 제약이 있다).
    milestones = [
        MilestoneSnapshot(
            id=m["id"],
            goal=m.get("goal", ""),
            target_date=parse_date(m.get("target_date")),
            completed=bool(m.get("completed")),
            is_overdue=bool(m.get("is_overdue")),
            is_next=bool(m.get("is_next")),
        )
        for m in await project_query._milestones(project_id)
    ]

    # 회의록은 "하기로 한 것" 대비 진행을 보는 근거다. 프로젝트 팀원이 다 보는 문서라 범위 문제도 없다.
    meetings = [
        MeetingSnapshot(
            id=m["id"],
            title=m.get("title", ""),
            meeting_date=parse_date(m.get("meeting_date")),
            purpose=m.get("purpose"),
            content=m.get("content"),
            follow_up=m.get("follow_up"),
            attendee_names=m.get("attendee_names") or [],
        )
        for m in await project_query._meetings(project_id)
    ]

    return ProjectSnapshot(
        id=raw["id"],
        name=raw.get("name", ""),
        status=raw.get("status", "ACTIVE"),
        start_date=parse_date(raw.get("start_date")),
        due_date=parse_date(raw.get("due_date")),
        members=members,
        todos=todos,
        milestones=milestones,
        meetings=meetings,
    )


async def _load_budget(project_id: int) -> BudgetSnapshot | None:
    raw = await budget_tool.fetch_budget(project_id)
    if not raw:
        return None
    return BudgetSnapshot(
        project_id=project_id,
        total=int(raw.get("total", 0) or 0),
        spent=int(raw.get("spent", 0) or 0),
        committed=int(raw.get("committed", 0) or 0),
        currency=raw.get("currency", "KRW"),
    )


async def _load_my_week(
    context: AnalysisContext, window_from: date, window_to: date
) -> MyWeekSnapshot | None:
    """요청자 본인의 주간 스냅샷. 전부 본인 스코프 내부도구라 남의 데이터가 섞일 수 없다."""
    weekly = await hr_tool.fetch_my_tasks()
    if weekly is None:
        return None

    balance = await hr_tool.fetch_my_leave_balance(context.as_of.year) or {}
    schedules = await hr_tool.fetch_my_schedules(window_from, window_to)

    return MyWeekSnapshot(
        week_start=parse_date(weekly.get("week_start")),
        week_end=parse_date(weekly.get("week_end")),
        tasks=[
            TodoSnapshot(
                id=t["id"],
                project_id=t.get("project_id"),
                title=t.get("title", ""),
                status=t.get("status", "TODO"),
                due_date=parse_date(t.get("due_date")),
            )
            for t in weekly.get("tasks", [])
            if t.get("id") is not None
        ],
        schedules=schedules,
        leave_granted_days=balance.get("granted"),
        leave_used_days=balance.get("used"),
        leave_remaining_days=balance.get("remaining"),
    )


async def _load_people(
    plan: AnalysisPlan, context: AnalysisContext, window_from: date, window_to: date
) -> None:
    """후보군 · 승인 휴가 · 가용성 지표를 채운다.

    ★ 후보군은 **프로젝트 참여자 + 질문에 이름이 나온 사람** 뿐이다.
      예전에는 후보가 없으면 전사 명부를 긁었고(`list_department_members`), 그 경로로
      프로젝트와 무관한 사람이 분석에 섞였다. BE 가 `/users` 에 keyword 를 필수로 걸어
      막아둔 조회이기도 하다. 후보가 없으면 긁지 말고 Data Gate 가 축을 건너뛴다.
    """
    candidates: dict[int, MemberSnapshot] = {}

    # 1) 프로젝트 참여자
    for project in context.projects:
        for member in project.members:
            candidates.setdefault(member.user_id, member)

    # 2) 질문에 이름이 나온 사람 (user.search — 이름 검색만 열려 있다)
    for name in plan.entities.user_names:
        user = await hr_tool.fetch_user(name=name)
        if user:
            candidates.setdefault(user["id"], _member_from_user(user))

    # 3) 질문에 id 로 나온 사람은 프로젝트 참여자 안에서만 찾는다 (남의 프로필을 여는 경로 없음)
    for user_id in plan.entities.user_ids:
        if user_id not in candidates:
            context.missing.append(f"참여자 밖의 구성원(id={user_id})은 조회하지 않음")

    context.candidates = list(candidates.values())
    if not context.candidates:
        return

    user_ids = [m.user_id for m in context.candidates]
    if len(user_ids) > hr_tool._MAX_TARGET_USERS:
        context.notes.append(
            f"참여자 {len(user_ids)}명 중 앞 {hr_tool._MAX_TARGET_USERS}명만 가용성을 확인했다"
        )

    # 휴가·일정은 내부도구가 userIds 를 한 번에 받는다 — 인원별 반복 호출을 하지 않는다
    leaves = await hr_tool.fetch_leaves(user_ids, window_from, window_to)
    schedules = await hr_tool.fetch_schedules(user_ids, window_from, window_to)

    name_by_id = {m.user_id: m.name for m in context.candidates}
    for leave in leaves:
        context.leaves.append(
            LeaveSnapshot(
                id=leave.get("id") or 0,
                user_id=leave["user_id"],
                user_name=leave.get("user_name") or name_by_id.get(leave["user_id"]),
                leave_type=leave.get("leave_type", "연차"),
                start_date=parse_date(leave.get("start_date")),
                end_date=parse_date(leave.get("end_date")),
            )
        )

    if not leaves and not schedules:
        context.missing.append("휴가·일정 조회 결과 없음 — 부재 판단 불가")

    # 셀 수 있는 것은 코드가 센다. LLM 에게는 '그래서 문제인가'만 묻는다.
    context.workloads = [
        _availability(context, member, window_from, window_to, schedules)
        for member in context.candidates
    ]


def _availability(
    context: AnalysisContext,
    member: MemberSnapshot,
    window_from: date,
    window_to: date,
    schedules: list[dict],
) -> dict[str, Any]:
    """한 사람의 기간 내 가용성 지표.

    할 일은 컨텍스트에 이미 실린 **프로젝트 할 일**만 센다 — 프로젝트 밖 할 일은
    요청자가 화면에서 볼 수 없는 정보라 조회하지 않는다.
    """
    todos = [
        t
        for project in context.projects
        for t in project.open_todos
        if t.assignee_id == member.user_id
    ]

    overdue = [t for t in todos if t.due_date and t.due_date < context.as_of]
    due_in_window = [
        t for t in todos if t.due_date and window_from <= t.due_date <= window_to
    ]

    leave_days = 0
    for leave in context.leaves:
        if leave.user_id != member.user_id or not (leave.start_date and leave.end_date):
            continue
        span_start, span_end = max(leave.start_date, window_from), min(leave.end_date, window_to)
        if span_start <= span_end:
            leave_days += (span_end - span_start).days + 1

    meeting_hours = 0.0
    for schedule in schedules:
        if member.name and member.name not in (schedule.get("participant_names") or []):
            continue
        meeting_hours += _hours_between(schedule.get("start_at"), schedule.get("end_at"))

    working_days = _working_days(window_from, window_to)
    available_days = max(0, working_days - leave_days)

    return {
        "user_id": member.user_id,
        "name": member.name,
        "department": member.department,
        "position": member.position,
        "window": {"from": window_from.isoformat(), "to": window_to.isoformat()},
        "open_todo_count": len(todos),
        "overdue_count": len(overdue),
        "due_in_window_count": len(due_in_window),
        "overdue_tasks": [{"id": t.id, "title": t.title, "due_date": t.due_date} for t in overdue],
        "due_in_window_tasks": [
            {"id": t.id, "title": t.title, "due_date": t.due_date} for t in due_in_window
        ],
        "approved_leave_days": leave_days,
        "meeting_hours": round(meeting_hours, 1),
        "working_days": working_days,
        "available_days": available_days,
        # 마감 건수 대비 실제 가용일. 사람 평가가 아니라 기간이 빠듯한지를 보는 값이다.
        "load_index": round(len(due_in_window) / available_days, 2) if available_days else None,
    }


def _hours_between(start_at: Any, end_at: Any) -> float:
    """"2026-08-06T10:00:00" 두 개의 시간 차(시간). 파싱 실패는 0."""
    try:
        start, end = datetime.fromisoformat(str(start_at)), datetime.fromisoformat(str(end_at))
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (end - start).total_seconds() / 3600)


def _working_days(start: date, end: date) -> int:
    """주말만 제외한 근무일 수. (공휴일 테이블은 아직 없다)"""
    if end < start:
        return 0
    days, current = 0, start
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current = date.fromordinal(current.toordinal() + 1)
    return days


def _member_from_user(user: dict[str, Any] | None) -> MemberSnapshot | None:
    if not user:
        return None
    return MemberSnapshot(
        user_id=user["id"],
        name=user.get("name", ""),
        department=user.get("department"),
        position=user.get("position"),
        role=user.get("role"),
        status=user.get("status"),
    )


# ─── 프롬프트용 렌더링 ────────────────────────────────────────────

# 워커별로 필요한 섹션만 넣어 토큰을 아낀다.
ALL_SECTIONS = (
    "project", "milestones", "todos", "members", "meetings",
    "budget", "leaves", "workload", "candidates", "my_week",
)


def render_context(
    context: AnalysisContext,
    sections: Iterable[str] = ALL_SECTIONS,
    scenario: ScenarioSpec | None = None,
) -> str:
    """컨텍스트를 마크다운으로. 워커 프롬프트의 [컨텍스트] 블록이 된다."""
    wanted = set(sections)
    lines: list[str] = [f"오늘: {context.as_of.isoformat()}"]

    if context.window_from and context.window_to:
        lines.append(
            f"분석 기간: {context.window_from.isoformat()} ~ {context.window_to.isoformat()}"
        )
    if context.requester:
        lines.append(
            f"요청자: {context.requester.name}({context.requester.user_id}) "
            f"/ {context.requester.department} {context.requester.position}"
        )

    if scenario and scenario.scenario_id != "base":
        lines += [
            "",
            f"## 적용할 조정안: {scenario.label}",
            scenario.description or "(설명 없음)",
        ]
        for key, value in (scenario.overrides or {}).items():
            lines.append(f"- {key}: {value}")
        lines.append("이 조정이 적용된 상태를 전제로 분석하라.")

    for project in context.projects:
        if "project" in wanted:
            lines += ["", f"## 프로젝트 {project.id} · {project.name}"]
            d_day = (
                (project.due_date - context.as_of).days if project.due_date else None
            )
            lines.append(
                f"- 상태 {project.status} | 기간 {project.start_date} ~ {project.due_date}"
                + (f" | 목표일까지 {d_day}일" if d_day is not None else "")
            )
            # 진척률은 마일스톤이 1차 근거다. 할 일 비율은 주 단위 조회라 부분집계일 수 있다.
            milestone_progress = project.milestone_progress
            if milestone_progress is not None:
                done = sum(1 for m in project.milestones if m.completed)
                lines.append(
                    f"- 마일스톤 진척 {milestone_progress:.1%} ({done}/{len(project.milestones)})"
                )
            counted = [t for t in project.todos if t.status != "CANCELED"]
            done_todos = sum(1 for t in counted if t.status == "DONE")
            lines.append(
                f"- 할 일 완료 {project.progress:.1%} ({done_todos}/{len(counted)})"
                + ("" if milestone_progress is not None else " ← 마일스톤이 없어 이 값이 유일한 진척 근거다")
            )
            lines.append(f"- 열린 할 일 {len(project.open_todos)}건")

        if "milestones" in wanted and project.milestones:
            lines += [
                "",
                "### 마일스톤",
                "| id | 목표 | 목표일 | 완료 | 지연 | 다음 |",
                "|---|---|---|---|---|---|",
            ]
            for milestone in sorted(
                project.milestones,
                key=lambda m: (m.target_date is None, m.target_date or context.as_of),
            ):
                lines.append(
                    f"| {milestone.id} | {milestone.goal} | {milestone.target_date or '-'} "
                    f"| {'O' if milestone.completed else '-'} "
                    f"| {'지연' if milestone.is_overdue and not milestone.completed else '-'} "
                    f"| {'다음' if milestone.is_next else '-'} |"
                )

        if "members" in wanted and project.members:
            lines += ["", "### 참여자", "| user_id | 이름 | 역할 | 부서 | 직책 |", "|---|---|---|---|---|"]
            for member in project.members:
                lines.append(
                    f"| {member.user_id} | {member.name} | {member.role or '-'} "
                    f"| {member.department or '-'} | {member.position or '-'} |"
                )

        if "todos" in wanted and project.todos:
            lines += [
                "",
                "### 할 일",
                "| id | 제목 | 상태 | 마감 | D-day | 담당 |",
                "|---|---|---|---|---|---|",
            ]
            for todo in sorted(
                project.todos, key=lambda t: (t.due_date is None, t.due_date or context.as_of)
            ):
                d_day = (todo.due_date - context.as_of).days if todo.due_date else None
                d_text = "-" if d_day is None else (f"D+{-d_day} 지연" if d_day < 0 else f"D-{d_day}")
                lines.append(
                    f"| {todo.id} | {todo.title} | {todo.status} | {todo.due_date or '-'} "
                    f"| {d_text} | {todo.assignee_name or todo.assignee_id or '-'} |"
                )

        if "meetings" in wanted and project.meetings:
            lines += ["", "### 최근 회의록 (하기로 한 것 대비 진행을 볼 때 쓴다)"]
            for meeting in project.meetings:
                # id 를 반드시 낸다 — 없으면 followup 축이 회의 번호를 1, 2 로 지어낸다.
                lines.append(
                    f"- meeting:{meeting.id} [{meeting.meeting_date or '날짜 미상'}] {meeting.title}"
                    + (f" — 목적: {meeting.purpose}" if meeting.purpose else "")
                )
                if meeting.content:
                    lines.append(f"  - 내용: {meeting.content[:300]}")
                if meeting.follow_up:
                    lines.append(f"  - 후속 조치: {meeting.follow_up[:300]}")

        if "budget" in wanted:
            budget = context.budget(project.id)
            if budget:
                lines += [
                    "",
                    "### 예산",
                    f"- 총액 {budget.total:,} / 집행 {budget.spent:,} / 결재중 {budget.committed:,}",
                    f"- 잔액 {budget.remaining:,} (소진율 {budget.usage_ratio:.1%})",
                ]
                if not budget.committed:
                    # 내부도구에 결재 조회가 없어 committed 가 늘 0이다. 잔액이 실제보다 커 보인다.
                    lines.append("- ⚠ 결재 대기 금액 미반영 — 실제 잔액은 이보다 적을 수 있다")

    if "leaves" in wanted and context.leaves:
        lines += ["", "## 승인된 휴가", "| user_id | 이름 | 종류 | 기간 |", "|---|---|---|---|"]
        for leave in context.leaves:
            lines.append(
                f"| {leave.user_id} | {leave.user_name or '-'} | {leave.leave_type} "
                f"| {leave.start_date} ~ {leave.end_date} |"
            )

    if "workload" in wanted and context.workloads:
        lines += [
            "",
            "## 가용성 지표 (코드 계산값 — 그대로 인용할 것)",
            "프로젝트 할 일 기준이다. 사람을 평가하는 값이 아니라 **어느 기간이 빠듯한지**를 보는 값이다.",
            "| user_id | 이름 | 열린일 | 지연 | 기간내마감 | 휴가일 | 회의h | 근무일 | 가용일 | load_index |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for workload in context.workloads:
            lines.append(
                f"| {workload['user_id']} | {workload.get('name') or '-'} "
                f"| {workload['open_todo_count']} | {workload['overdue_count']} "
                f"| {workload['due_in_window_count']} | {workload['approved_leave_days']} "
                f"| {workload['meeting_hours']} | {workload['working_days']} "
                f"| {workload['available_days']} | {workload.get('load_index')} |"
            )

    if "candidates" in wanted and context.candidates:
        lines += [
            "",
            "## 후보군 (프로젝트 참여자 + 질문에 이름이 나온 사람 뿐이다)",
            "| user_id | 이름 | 역할 | 부서 | 직책 |",
            "|---|---|---|---|---|",
        ]
        for candidate in context.candidates:
            lines.append(
                f"| {candidate.user_id} | {candidate.name} | {candidate.role or '-'} "
                f"| {candidate.department or '-'} | {candidate.position or '-'} |"
            )
        lines.append(
            "이 표 밖의 사람은 존재 여부조차 알 수 없다. 다른 이름을 만들어내지 마라."
        )

    if "my_week" in wanted and context.my_week:
        week = context.my_week
        lines += ["", f"## 내 이번 주 ({week.week_start} ~ {week.week_end})"]
        if week.leave_remaining_days is not None:
            lines.append(
                f"- 연차: 부여 {week.leave_granted_days} / 사용 {week.leave_used_days} "
                f"/ 남음 {week.leave_remaining_days}일"
            )
        if week.tasks:
            lines += ["", "| id | 제목 | 상태 | 마감 | D-day |", "|---|---|---|---|---|"]
            for task in sorted(
                week.tasks, key=lambda t: (t.due_date is None, t.due_date or context.as_of)
            ):
                d_day = (task.due_date - context.as_of).days if task.due_date else None
                d_text = "-" if d_day is None else (f"D+{-d_day} 지연" if d_day < 0 else f"D-{d_day}")
                lines.append(
                    f"| {task.id} | {task.title} | {task.status} "
                    f"| {task.due_date or '-'} | {d_text} |"
                )
        else:
            lines.append("- 이번 주 할 일 없음")
        if week.schedules:
            lines.append("")
            lines += [
                f"- 일정: {s.get('title')} {s.get('start_at')} ~ {s.get('end_at')}"
                for s in week.schedules[:10]
            ]

    if context.missing:
        lines += ["", "## 확보하지 못한 정보 (없는 값을 추정으로 채우지 말 것)"]
        lines += [f"- {item}" for item in context.missing]

    if context.skipped:
        lines += ["", "## 근거 부족으로 분석하지 않는 축"]
        lines += [f"- {item}" for item in context.skipped]

    return "\n".join(lines)
