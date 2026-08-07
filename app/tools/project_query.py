# app/tools/project_query.py
"""프로젝트 도메인 읽기 툴 (Engine B project 워커용).

    담당자 1의 project_tool 과는 별개다. 그쪽은 내부 API 규격에 맞춘 비동기 도구고,
    여기는 워커가 동기로 부르는 분석용 조회다.

워커가 컨텍스트만으로 판단이 어려울 때 스스로 호출한다. (워커당 최대 5회)
전부 읽기 전용이므로 HITL 승인 대상이 아니다.

조회 창구는 clients/backend.py 하나다 (2026-08-07 통일 — X-Run-Id 는 run_context 로 전파).
mock 모드·run_id 부재·호출 실패면 demo_data 픽스처로 폴백한다.
프로젝트 상세(project.detail)는 ⛔ v1 제외라 목록(project.search)에서 집어 쓴다.
"""

import json
from typing import Any

from langchain_core.tools import tool

from app.clients.backend import backend
from app.common.run_context import current_run_id
from app.config import get_settings
from app.tools import demo_data
from app.utils.logger import get_logger

log = get_logger("tools.project")


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _get(path: str, **params: Any) -> Any | None:
    """실백엔드 조회. mock 모드·run_id 부재·실패면 None — 호출부가 픽스처로 폴백한다."""
    if get_settings().uses_fixtures:
        return None
    run_id = current_run_id.get()
    if not run_id:
        log.warning("run_id 없이 내부도구 호출: %s — 픽스처 폴백", path)
        return None
    try:
        return await backend.get(path, run_id, **params)
    except Exception as exc:  # 조회 실패로 분석을 죽이지 않는다 (폴백 원칙)
        log.warning("backend GET %s 실패 -> 픽스처 폴백: %s", path, exc)
        return None


def _task(t: dict) -> dict:
    """task.list 항목 → 픽스처 모양. completed(bool)라 상태는 TODO/DONE 2값만 나온다."""
    return {"id": t.get("taskId"), "title": t.get("content"), "due_date": t.get("dueDate"),
            "status": "DONE" if t.get("completed") else "TODO",
            "assignee_id": t.get("assigneeId"), "project_id": t.get("projectId")}


# ─── 조회 툴 (워커가 자율 호출) ───────────────────────────────────

@tool
async def get_project_overview(project_id: int = 0, project_name: str = "") -> str:
    """프로젝트 기본 정보(상태·시작일·목표일·참여자·진행률)를 조회한다.

    project_id 를 모르면 project_name 에 프로젝트명 일부를 넣어도 된다.
    """
    project = await fetch_project(project_id or None, project_name or None)
    if not project:
        return _json({"error": "해당 프로젝트를 찾지 못했습니다.", "project_id": project_id, "project_name": project_name})

    pid = project["id"]
    members = await _members(pid)
    todos = await _todos(pid)
    counted = [t for t in todos if t.get("status") != "CANCELED"]
    done = sum(1 for t in counted if t.get("status") == "DONE")
    return _json(
        {
            **project,
            "member_count": len(members),
            "members": members,
            "todo_total": len(counted),
            "todo_done": done,
            "progress": round(done / len(counted), 3) if counted else 0.0,
        }
    )


@tool
async def list_project_tasks(project_id: int, status: str = "", assignee_id: int = 0) -> str:
    """프로젝트의 할 일 목록을 조회한다.

    status 는 TODO / IN_PROGRESS / DONE / CANCELED 중 하나(비우면 전체),
    assignee_id 를 주면 특정 담당자 것만 조회한다.
    """
    todos = await _todos(project_id, status=status or None, assignee_id=assignee_id or None)
    return _json({"project_id": project_id, "count": len(todos), "tasks": todos})


@tool
async def list_project_members(project_id: int) -> str:
    """프로젝트 참여자와 각자의 역할(PM/BE/FE/QA/DESIGN/INFRA)을 조회한다."""
    members = await _members(project_id)
    return _json({"project_id": project_id, "count": len(members), "members": members})


@tool
async def find_projects(user_id: int = 0, status: str = "") -> str:
    """프로젝트 목록을 조회한다. user_id 를 주면 그 사람이 참여 중인 것만 조회한다.

    다른 프로젝트와의 인력·일정 충돌을 볼 때 쓴다.
    """
    raw = await _get("/projects")  # project.search — 요청자 참여 프로젝트 스코프
    if raw is None:
        projects = demo_data.list_projects(user_id=user_id or None, status=status or None)
    else:
        projects = [
            {"id": p.get("projectId"), "name": p.get("name"), "status": p.get("status"),
             "start_date": p.get("startDate"), "due_date": p.get("targetDate")}
            for p in raw.get("projects", [])
        ]
        if status:
            projects = [p for p in projects if p.get("status") == status]
        # 타인(user_id) 필터는 내부도구 명세 없음 — 요청자 스코프 결과를 그대로 쓴다
    return _json({"count": len(projects), "projects": projects})


# ─── 내부 헬퍼 - Context Builder 도 재사용한다 (툴 래핑 없이 직접 호출) ────────

async def _members(project_id: int) -> list[dict]:
    raw = await _get(f"/projects/{project_id}/members")
    if raw is None:
        return demo_data.list_project_members(project_id)
    return [
        {"user_id": m.get("userId"), "name": m.get("name"), "role": m.get("role"),
         "department": m.get("department"), "position": m.get("position")}
        for m in raw.get("members", [])
    ]


async def _todos(
    project_id: int, status: str | None = None, assignee_id: int | None = None
) -> list[dict]:
    raw = await _get("/tasks", projectId=project_id)
    if raw is None:
        return demo_data.list_todos(
            project_id=project_id, status=status, assignee_id=assignee_id
        )
    tasks = [_task(t) for t in raw.get("tasks", [])]
    if status:  # 내부도구는 상태 필터가 없어 클라이언트에서 거른다
        tasks = [t for t in tasks if t.get("status") == status]
    if assignee_id:
        tasks = [t for t in tasks if t.get("assignee_id") == assignee_id]
    return tasks


async def fetch_project(project_id: int | None, project_name: str | None = None) -> dict | None:
    raw = await _get("/projects")  # project.detail 은 ⛔ v1 제외 — 목록에서 집는다
    if raw is None:
        return demo_data.get_project(project_id=project_id, name=project_name)
    for p in raw.get("projects", []):
        if (project_id and p.get("projectId") == project_id) or \
           (project_name and project_name in (p.get("name") or "")):
            return {"id": p.get("projectId"), "name": p.get("name"), "status": p.get("status"),
                    "start_date": p.get("startDate"), "due_date": p.get("targetDate")}
    return None  # 실백엔드가 답했는데 없음 — 픽스처로 가짜를 만들지 않는다


PROJECT_TOOLS = [
    get_project_overview,
    list_project_tasks,
    list_project_members,
    find_projects,
]
