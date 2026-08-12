# app/tests/fixtures/stub.py
"""픽스처를 내부도구 **자리에** 끼우는 설치기.

프로덕션 코드에는 픽스처 분기가 없다 (2026-08-11). 조회에 실패하면 빈 결과이고,
호출부가 "확인 못 함"으로 처리한다. 그래서 백엔드 없이 돌려보려면 픽스처를
분기로 넣는 게 아니라 **API 자리에 끼워** '백엔드가 답한 상태'를 만들어야 한다.

테스트(conftest)와 수동 실행기(engine_b/demo.py)가 같이 쓴다.
"""

from datetime import date

from app.tests.fixtures import demo_data

_NAMES = {u["id"]: u["name"] for u in demo_data.USERS}


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, date) else value


# ─── 내부도구 응답 모양으로 변환 ──────────────────────────────────

def _members_payload(project_id: int) -> dict:
    return {
        "members": [
            {"userId": m["id"], "name": m.get("name"), "role": m.get("role"),
             "department": m.get("department"), "position": m.get("position")}
            for m in demo_data.list_project_members(project_id)
        ]
    }


def _tasks_payload(project_id: int) -> dict:
    return {
        "tasks": [
            {"taskId": t["id"], "content": t.get("title"), "dueDate": _iso(t.get("due_date")),
             "completed": t.get("status") == "DONE", "assigneeId": t.get("assignee_id"),
             "projectId": t.get("project_id")}
            for t in demo_data.list_todos(project_id=project_id)
        ]
    }


def _projects_payload(requester_id: int) -> dict:
    """project.search 는 **요청자 참여분만** 준다 — 스텁도 그 스코프를 지킨다.

    여기서 전체 목록을 주면 실제보다 넓은 결과로 테스트가 통과해버린다.
    """
    return {
        "projects": [
            {"projectId": p["id"], "name": p.get("name"), "status": p.get("status"),
             "startDate": _iso(p.get("start_date")), "targetDate": _iso(p.get("due_date"))}
            for p in demo_data.list_projects(user_id=requester_id)
        ]
    }


def _budget_payload(project_id: int) -> dict | None:
    budget = demo_data.get_budget(project_id)
    if not budget:
        return None
    return {"targetBudget": budget.get("total"), "spentAmount": budget.get("spent"),
            "committed": budget.get("committed")}


def _expenses_payload(project_id: int) -> dict:
    return {
        "expenses": [
            {"expenseId": e["id"], "expenseDate": _iso(e.get("date")),
             "categoryLabel": e.get("category"), "amount": e.get("amount"),
             "purpose": e.get("title")}
            for e in demo_data.list_expenses(project_id)
        ]
    }


def _leaves(user_ids, date_from, date_to) -> list[dict]:
    return [
        leave
        for uid in user_ids
        for leave in demo_data.list_leaves(
            user_id=uid, date_from=date_from, date_to=date_to, status="APPROVED"
        )
    ]


def _schedules(user_ids, date_from, date_to) -> list[dict]:
    targets = set(user_ids)
    return [
        {"id": s["id"], "title": s["title"],
         "start_at": f"{s['date']}T{s['start_time']}:00",
         "end_at": f"{s['date']}T{s['end_time']}:00",
         "all_day": False,
         "participant_names": [_NAMES.get(p, "") for p in s["participants"]]}
        for s in demo_data.list_schedules(date_from=date_from, date_to=date_to)
        if targets & set(s["participants"])
    ]


# ─── 설치 ─────────────────────────────────────────────────────────

def install(setattr_fn, requester_id: int = 1) -> None:
    """툴 계층의 조회 지점을 픽스처로 덮는다.

    setattr_fn 은 monkeypatch.setattr 이거나 그와 같은 시그니처의 함수다
    (테스트는 monkeypatch, 수동 실행기는 내장 setattr).
    requester_id 는 실백엔드가 X-Run-Id 로 역산하는 요청자에 해당한다 —
    project.search 스코프를 그 사람 기준으로 맞춘다.
    """
    from app.tools import budget_tool, hr_tool, project_query

    async def project_get(path: str, **params):
        if path == "/projects":
            return _projects_payload(requester_id)
        if path.endswith("/members"):
            return _members_payload(int(path.split("/")[2]))
        if path == "/tasks":
            return _tasks_payload(int(params.get("projectId") or 0))
        if path.endswith("/milestones"):
            return {"milestones": []}      # 픽스처에 마일스톤이 없다 — 없는 그대로 낸다
        if path.endswith("/meetings"):
            return {"meetings": []}
        return None

    async def budget_get(path: str, **params):
        project_id = int(path.split("/")[2])
        if path.endswith("/budget"):
            return _budget_payload(project_id)
        if path.endswith("/expenses"):
            return _expenses_payload(project_id)
        return None

    async def leaves(user_ids, date_from, date_to):
        return _leaves(user_ids, date_from, date_to)

    async def schedules(user_ids, date_from, date_to):
        return _schedules(user_ids, date_from, date_to)

    setattr_fn(project_query, "_get", project_get)
    setattr_fn(budget_tool, "_get", budget_get)
    setattr_fn(hr_tool, "fetch_leaves", leaves)
    setattr_fn(hr_tool, "fetch_schedules", schedules)
