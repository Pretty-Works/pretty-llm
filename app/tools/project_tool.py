# app/tools/project_tool.py
"""프로젝트 도메인 읽기 툴 (Engine B project 워커용).

워커가 컨텍스트만으로 판단이 어려울 때 스스로 호출한다. (워커당 최대 5회)
전부 읽기 전용이므로 HITL 승인 대상이 아니다.

백엔드가 떠 있으면 REST 를, 아니면 app/tools/demo_data.py 픽스처를 읽는다.
"""

import json
from typing import Any

from langchain_core.tools import tool

from app.common import backend_client
from app.common.backend_client import BackendUnavailable
from app.tools import demo_data
from app.utils.logger import get_logger

log = get_logger("tools.project")


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


# ─── 조회 툴 (워커가 자율 호출) ───────────────────────────────────

@tool
def get_project_overview(project_id: int = 0, project_name: str = "") -> str:
    """프로젝트 기본 정보(상태·시작일·목표일·참여자·진행률)를 조회한다.

    project_id 를 모르면 project_name 에 프로젝트명 일부를 넣어도 된다.
    """
    try:
        if project_id:
            project = backend_client.get(f"/api/v1/projects/{project_id}")
        else:
            raise BackendUnavailable("project_id 없이 백엔드 조회 불가")
    except BackendUnavailable:
        project = demo_data.get_project(project_id=project_id or None, name=project_name or None)

    if not project:
        return _json({"error": "해당 프로젝트를 찾지 못했습니다.", "project_id": project_id, "project_name": project_name})

    pid = project["id"]
    members = _members(pid)
    todos = _todos(pid)
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
def list_project_tasks(project_id: int, status: str = "", assignee_id: int = 0) -> str:
    """프로젝트의 할 일 목록을 조회한다.

    status 는 TODO / IN_PROGRESS / DONE / CANCELED 중 하나(비우면 전체),
    assignee_id 를 주면 특정 담당자 것만 조회한다.
    """
    todos = _todos(project_id, status=status or None, assignee_id=assignee_id or None)
    return _json({"project_id": project_id, "count": len(todos), "tasks": todos})


@tool
def list_project_members(project_id: int) -> str:
    """프로젝트 참여자와 각자의 역할(PM/BE/FE/QA/DESIGN/INFRA)을 조회한다."""
    members = _members(project_id)
    return _json({"project_id": project_id, "count": len(members), "members": members})


@tool
def find_projects(user_id: int = 0, status: str = "") -> str:
    """프로젝트 목록을 조회한다. user_id 를 주면 그 사람이 참여 중인 것만 조회한다.

    다른 프로젝트와의 인력·일정 충돌을 볼 때 쓴다.
    """
    try:
        params = {k: v for k, v in {"userId": user_id, "status": status}.items() if v}
        projects = backend_client.get("/api/v1/projects", params=params)
    except BackendUnavailable:
        projects = demo_data.list_projects(user_id=user_id or None, status=status or None)
    return _json({"count": len(projects), "projects": projects})


# ─── 내부 헬퍼 - Context Builder 도 재사용한다 (툴 래핑 없이 직접 호출) ────────

def _members(project_id: int) -> list[dict]:
    try:
        return backend_client.get(f"/api/v1/projects/{project_id}/members")
    except BackendUnavailable:
        return demo_data.list_project_members(project_id)


def _todos(
    project_id: int, status: str | None = None, assignee_id: int | None = None
) -> list[dict]:
    try:
        params = {k: v for k, v in {"status": status, "assigneeId": assignee_id}.items() if v}
        return backend_client.get(f"/api/v1/projects/{project_id}/tasks", params=params)
    except BackendUnavailable:
        return demo_data.list_todos(
            project_id=project_id, status=status, assignee_id=assignee_id
        )


def fetch_project(project_id: int | None, project_name: str | None = None) -> dict | None:
    try:
        if not project_id:
            raise BackendUnavailable("project_id 없음")
        return backend_client.get(f"/api/v1/projects/{project_id}")
    except BackendUnavailable:
        return demo_data.get_project(project_id=project_id, name=project_name)


PROJECT_TOOLS = [
    get_project_overview,
    list_project_tasks,
    list_project_members,
    find_projects,
]
