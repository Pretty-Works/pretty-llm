# app/tools/hr_tool.py
"""인력(hcm) 도메인 읽기 툴 (Engine B skill_fit / workload 워커용).

ERD 에 skill 컬럼이 없으므로 '이 사람이 무엇을 잘하는가'는 저장된 값이 아니라
부서 · 직책 · 과거 프로젝트에서 맡은 role · 처리한 할 일 이력에서 **추론**해야 한다.
그래서 skill 조회 툴 대신 `get_user_project_history` 와 `list_user_tasks` 를 제공한다.

가용성은 ERD 원칙대로 '승인된 휴가'만 반영한다. (PENDING 은 제외)
"""

import json
from datetime import date
from typing import Any

from langchain_core.tools import tool

from app.common import backend_client
from app.common.backend_client import BackendUnavailable
from app.config import get_settings
from app.tools import demo_data
from app.utils.logger import get_logger
from app.utils.parser import parse_date

log = get_logger("tools.hr")


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


# ─── 조회 툴 (워커가 자율 호출) ───────────────────────────────────

@tool
def find_user(name: str = "", user_id: int = 0) -> str:
    """이름 또는 사번(user_id)으로 구성원 프로필(부서·직책·입사일)을 조회한다."""
    user = fetch_user(user_id=user_id or None, name=name or None)
    if not user:
        return _json({"error": "해당 구성원을 찾지 못했습니다.", "name": name, "user_id": user_id})
    return _json(user)


@tool
def list_department_members(department: str = "", position: str = "") -> str:
    """부서/직책으로 구성원 목록을 조회한다. 대체 인력 후보를 찾을 때 쓴다."""
    try:
        params = {k: v for k, v in {"department": department, "position": position}.items() if v}
        users = backend_client.get("/api/v1/users", params=params)
    except BackendUnavailable:
        users = demo_data.list_users(department=department or None, position=position or None)
    return _json({"count": len(users), "users": users})


@tool
def get_user_project_history(user_id: int) -> str:
    """그 사람이 참여한(했던) 프로젝트와 역할 이력을 조회한다.

    skill 데이터가 따로 없으므로, 적합도 판단의 핵심 근거는 이 이력이다.
    """
    try:
        history = backend_client.get(f"/api/v1/users/{user_id}/projects")
    except BackendUnavailable:
        history = demo_data.user_project_history(user_id)
    return _json({"user_id": user_id, "count": len(history), "history": history})


@tool
def list_user_tasks(user_id: int, status: str = "") -> str:
    """그 사람에게 배정된 할 일을 조회한다. 어떤 종류의 작업을 해왔는지 보는 용도."""
    try:
        params = {k: v for k, v in {"assigneeId": user_id, "status": status}.items() if v}
        todos = backend_client.get("/api/v1/tasks", params=params)
    except BackendUnavailable:
        todos = demo_data.list_todos(assignee_id=user_id, status=status or None)
    return _json({"user_id": user_id, "count": len(todos), "tasks": todos})


@tool
def list_user_leaves(user_id: int, date_from: str = "", date_to: str = "") -> str:
    """승인된 휴가 일정을 조회한다. (YYYY-MM-DD)

    기간을 주면 그 기간에 걸치는 휴가만 반환한다. 미승인 신청은 포함하지 않는다.
    """
    leaves = fetch_leaves(user_id, parse_date(date_from), parse_date(date_to))
    return _json({"user_id": user_id, "count": len(leaves), "leaves": leaves})


@tool
def get_leave_balance(user_id: int, year: int = 0) -> str:
    """잔여 연차를 조회한다. 부여일수에서 승인된 휴가일수를 뺀 값이다."""
    target_year = year or get_settings().as_of().year
    try:
        balance = backend_client.get(
            "/api/v1/calendar/leaves/balance", params={"userId": user_id, "year": target_year}
        )
    except BackendUnavailable:
        balance = demo_data.leave_balance(user_id, target_year)
    return _json(balance)


@tool
def get_user_workload(user_id: int, date_from: str = "", date_to: str = "") -> str:
    """지정 기간의 업무 부하를 집계한다.

    진행 중인 할 일 수, 기간 내 마감 건수, 지연 건수, 승인된 휴가일수,
    회의 시간을 함께 돌려준다. 특정인에게 일이 몰렸는지 판단할 때 쓴다.
    """
    settings = get_settings()
    start = parse_date(date_from) or settings.as_of()
    end = parse_date(date_to) or date(start.year, 12, 31)
    return _json(compute_workload(user_id, start, end))


# ─── 비-툴 진입점 (Context Builder / workload 워커가 직접 호출) ────────

def fetch_user(user_id: int | None = None, name: str | None = None) -> dict | None:
    try:
        if not user_id:
            raise BackendUnavailable("user_id 없음")
        return backend_client.get(f"/api/v1/users/{user_id}")
    except BackendUnavailable:
        return demo_data.get_user(user_id=user_id, name=name)


def fetch_leaves(
    user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    try:
        params = {
            k: v
            for k, v in {
                "userIds": user_id,
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None,
            }.items()
            if v
        }
        return backend_client.get("/api/v1/calendar/leaves", params=params)
    except BackendUnavailable:
        return demo_data.list_leaves(
            user_id=user_id, date_from=date_from, date_to=date_to, status="APPROVED"
        )


def fetch_schedules(
    user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[dict]:
    try:
        params = {
            k: v
            for k, v in {
                "userIds": user_id,
                "from": date_from.isoformat() if date_from else None,
                "to": date_to.isoformat() if date_to else None,
            }.items()
            if v
        }
        return backend_client.get("/api/v1/calendar/schedules", params=params)
    except BackendUnavailable:
        return demo_data.list_schedules(
            user_id=user_id, date_from=date_from, date_to=date_to
        )


def compute_workload(user_id: int, start: date, end: date) -> dict:
    """한 사람의 기간 내 부하 지표를 코드로 계산한다.

    LLM 에게 원자료를 통째로 넘겨 세게 하는 대신, 셀 수 있는 것은 코드로 세고
    워커는 '그래서 이게 과부하인가'를 판단하게 한다.
    """
    settings = get_settings()
    todos = demo_data.list_todos(assignee_id=user_id)
    try:
        todos = backend_client.get("/api/v1/tasks", params={"assigneeId": user_id})
    except BackendUnavailable:
        pass

    open_todos = [t for t in todos if t.get("status") in {"TODO", "IN_PROGRESS"}]
    overdue, due_in_window = [], []
    for todo in open_todos:
        due = parse_date(todo.get("due_date"))
        if due is None:
            continue
        if due < settings.as_of():
            overdue.append(todo)
        elif start <= due <= end:
            due_in_window.append(todo)

    leaves = fetch_leaves(user_id, start, end)
    leave_days = 0
    for leave in leaves:
        lstart, lend = parse_date(leave.get("start_date")), parse_date(leave.get("end_date"))
        if not lstart or not lend:
            continue
        # 조회 기간과 겹치는 부분만 센다
        span_start, span_end = max(lstart, start), min(lend, end)
        if span_start <= span_end:
            leave_days += (span_end - span_start).days + 1

    schedules = fetch_schedules(user_id, start, end)
    meeting_hours = 0.0
    for schedule in schedules:
        try:
            sh, sm = (int(x) for x in str(schedule.get("start_time", "0:0")).split(":"))
            eh, em = (int(x) for x in str(schedule.get("end_time", "0:0")).split(":"))
            meeting_hours += max(0.0, (eh * 60 + em - sh * 60 - sm) / 60)
        except (ValueError, TypeError):
            continue

    user = fetch_user(user_id=user_id)
    working_days = _working_days(start, end)
    available_days = max(0, working_days - leave_days)

    return {
        "user_id": user_id,
        "name": (user or {}).get("name"),
        "department": (user or {}).get("department"),
        "position": (user or {}).get("position"),
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "open_todo_count": len(open_todos),
        "overdue_count": len(overdue),
        "due_in_window_count": len(due_in_window),
        "overdue_tasks": [{"id": t["id"], "title": t.get("title"), "due_date": t.get("due_date")} for t in overdue],
        "due_in_window_tasks": [{"id": t["id"], "title": t.get("title"), "due_date": t.get("due_date")} for t in due_in_window],
        "approved_leave_days": leave_days,
        "meeting_hours": round(meeting_hours, 1),
        "working_days": working_days,
        "available_days": available_days,
        # 마감 임박 건수 대비 실제 가용일. 1.0 을 넘으면 물리적으로 빠듯하다는 뜻.
        "load_index": round(len(due_in_window) / available_days, 2) if available_days else None,
    }


def _working_days(start: date, end: date) -> int:
    """주말만 제외한 근무일 수. (공휴일 테이블은 아직 없음)"""
    if end < start:
        return 0
    days = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current = date.fromordinal(current.toordinal() + 1)
    return days


HR_TOOLS = [
    find_user,
    list_department_members,
    get_user_project_history,
    list_user_tasks,
    list_user_leaves,
    get_leave_balance,
    get_user_workload,
]
