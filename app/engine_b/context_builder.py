# app/engine_b/context_builder.py
"""Context Builder — 워커들이 공통으로 볼 사실을 한 번에 모은다.

워커 5개가 각자 같은 프로젝트/할 일/인원을 다시 조회하면 토큰과 시간이 그대로 5배가 된다.
그래서 공통분모는 여기서 코드로 한 번만 모으고, 워커는 부족한 것만 툴로 채운다.

셀 수 있는 것(진행률, 부하 지표, 잔여 예산)은 여기서 코드로 계산한다.
LLM 에게는 '그래서 이게 문제인가'만 묻는다.
"""

from datetime import date, timedelta
from typing import Any, Iterable

from app.config import get_settings
from app.schemas.state import (
    AnalysisContext,
    AnalysisPlan,
    AnalysisRequest,
    BudgetSnapshot,
    LeaveSnapshot,
    MemberSnapshot,
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
    context.requester = _member_from_user(await hr_tool.fetch_user(user_id=request.user_id))

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

    if not context.projects and not context.candidates:
        context.notes.append(
            "컨텍스트가 비어 있다. 워커는 도구로 직접 대상을 찾아야 한다."
        )

    log.info(
        "context: 프로젝트 %d건, 후보 %d명, 기간 %s~%s, 미확보 %d건",
        len(context.projects),
        len(context.candidates),
        window_from,
        window_to,
        len(context.missing),
    )
    return context


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
            hire_date=parse_date(m.get("hire_date")),
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

    return ProjectSnapshot(
        id=raw["id"],
        name=raw.get("name", ""),
        status=raw.get("status", "ACTIVE"),
        start_date=parse_date(raw.get("start_date")),
        due_date=parse_date(raw.get("due_date")),
        members=members,
        todos=todos,
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


async def _load_people(
    plan: AnalysisPlan, context: AnalysisContext, window_from: date, window_to: date
) -> None:
    """인력 후보군 · 승인 휴가 · 부하 지표를 채운다."""
    candidates: dict[int, MemberSnapshot] = {}

    # 1) 프로젝트 참여자
    for project in context.projects:
        for member in project.members:
            candidates.setdefault(member.user_id, member)

    # 2) 질문에 이름이 나온 사람
    for name in plan.entities.user_names:
        user = await hr_tool.fetch_user(name=name)
        if user:
            candidates.setdefault(user["id"], _member_from_user(user))
    for user_id in plan.entities.user_ids:
        user = await hr_tool.fetch_user(user_id=user_id)
        if user:
            candidates.setdefault(user_id, _member_from_user(user))

    # 3) 적임자 추천은 프로젝트 밖 인원도 봐야 한다
    if "skill_fit" in plan.focus or not candidates:
        import json

        try:
            payload = json.loads(await hr_tool.list_department_members.ainvoke({}))
            for user in payload.get("users", []):
                candidates.setdefault(user["id"], _member_from_user(user))
        except Exception as exc:
            log.warning("구성원 목록 조회 실패: %s", exc)
            context.missing.append("전사 구성원 목록 조회 실패")

    context.candidates = list(candidates.values())

    # 승인된 휴가 (기간 겹치는 것만)
    for member in context.candidates:
        for leave in await hr_tool.fetch_leaves(member.user_id, window_from, window_to):
            context.leaves.append(
                LeaveSnapshot(
                    id=leave.get("id", ""),
                    user_id=leave["user_id"],
                    user_name=leave.get("user_name") or member.name,
                    leave_type=leave.get("leave_type", "연차"),
                    start_date=parse_date(leave.get("start_date")),
                    end_date=parse_date(leave.get("end_date")),
                    status=leave.get("status", "APPROVED"),
                )
            )

    # 부하 지표는 코드로 계산해서 넣는다 (LLM 이 세지 않게)
    context.workloads = [
        await hr_tool.compute_workload(member.user_id, window_from, window_to)
        for member in context.candidates
    ]


def _member_from_user(user: dict[str, Any] | None) -> MemberSnapshot | None:
    if not user:
        return None
    return MemberSnapshot(
        user_id=user["id"],
        name=user.get("name", ""),
        department=user.get("department"),
        position=user.get("position"),
        role=user.get("role"),
        hire_date=parse_date(user.get("hire_date")),
        status=user.get("status"),
    )


# ─── 프롬프트용 렌더링 ────────────────────────────────────────────

# 워커별로 필요한 섹션만 넣어 토큰을 아낀다.
ALL_SECTIONS = ("project", "todos", "members", "budget", "leaves", "workload", "candidates")


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
            counted = [t for t in project.todos if t.status != "CANCELED"]
            done = sum(1 for t in counted if t.status == "DONE")
            lines.append(f"- 진행률 {project.progress:.1%} ({done}/{len(counted)})")
            lines.append(f"- 열린 할 일 {len(project.open_todos)}건")

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

        if "budget" in wanted:
            budget = context.budget(project.id)
            if budget:
                lines += [
                    "",
                    "### 예산",
                    f"- 총액 {budget.total:,} / 집행 {budget.spent:,} / 결재중 {budget.committed:,}",
                    f"- 잔액 {budget.remaining:,} (소진율 {budget.usage_ratio:.1%})",
                ]

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
            "## 인력 부하 지표 (코드 계산값 — 그대로 인용할 것)",
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
        lines += ["", "## 인력 후보군", "| user_id | 이름 | 부서 | 직책 | 입사일 |", "|---|---|---|---|---|"]
        for candidate in context.candidates:
            lines.append(
                f"| {candidate.user_id} | {candidate.name} | {candidate.department or '-'} "
                f"| {candidate.position or '-'} | {candidate.hire_date or '-'} |"
            )

    if context.missing:
        lines += ["", "## 확보하지 못한 정보 (필요하면 도구로 직접 조회할 것)"]
        lines += [f"- {item}" for item in context.missing]

    return "\n".join(lines)
