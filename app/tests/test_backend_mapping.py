# app/tests/test_backend_mapping.py
"""실백엔드 응답 → Engine B 내부 모양 매핑 검증.

배포에서만 터지고 로컬에서는 안 잡히던 구멍을 막는다 —
평소 테스트는 MOCK_BACKEND=true 라 픽스처 분기만 돌고, 실백엔드 분기
(`_get()` 이 값을 돌려준 경우)는 한 번도 실행되지 않았다. 실제로
`_members()` 만 `id` 대신 `user_id` 를 내보내 Context Builder 가
`KeyError: 'id'` 로 죽었고, Engine B 그래프 전체가 폴백으로 빠졌다.

내부 규약: 백엔드의 `projectId`·`userId`·`taskId` 는 경계에서 전부 `id` 로 바꾼다.
"""

import asyncio

from app.tools import project_query

# 실 Spring 내부 API 응답 형태 (공동 규격 · clients/backend.py 의 mock 과 같은 모양)
BACKEND = {
    "/projects": {"projects": [
        {"projectId": 3, "name": "그룹웨어 AI 고도화", "status": "ONGOING",
         "startDate": "2026-06-01", "targetDate": "2026-09-30"},
    ]},
    "/projects/3/members": {"members": [
        {"userId": 5, "name": "이하늘", "department": "백엔드개발", "position": "사원", "role": "백엔드"},
    ]},
    "/tasks": {"tasks": [
        {"taskId": 58, "content": "API 명세 정리", "dueDate": "2026-08-07",
         "completed": False, "assigneeId": 5, "projectId": 3},
    ]},
}


def _patch_backend(monkeypatch) -> None:
    """실백엔드가 응답한 상황을 만든다 (픽스처 폴백을 타지 않게)."""
    async def fake_get(path, **params):
        return BACKEND.get(path)

    monkeypatch.setattr(project_query, "_get", fake_get)


def test_실백엔드_멤버는_id_키로_넘어온다(monkeypatch) -> None:
    _patch_backend(monkeypatch)
    members = asyncio.run(project_query._members(3))
    assert members and all("id" in m for m in members), members
    assert members[0]["id"] == 5


def test_실백엔드_할일은_id_키로_넘어온다(monkeypatch) -> None:
    _patch_backend(monkeypatch)
    todos = asyncio.run(project_query._todos(3))
    assert todos and all("id" in t for t in todos), todos
    assert todos[0]["id"] == 58


def test_실백엔드_프로젝트는_id_키로_넘어온다(monkeypatch) -> None:
    _patch_backend(monkeypatch)
    project = asyncio.run(project_query.fetch_project(3))
    assert project and project["id"] == 3


def test_픽스처와_실백엔드가_같은_키를_쓴다(monkeypatch) -> None:
    """두 분기의 모양이 갈리면 배포에서만 터진다 — 공통 키를 강제한다."""
    from app.tests.fixtures import demo_data

    fixture_member = demo_data.list_project_members(1001)[0]
    _patch_backend(monkeypatch)
    real_member = asyncio.run(project_query._members(3))[0]

    for key in ("id", "name", "role"):
        assert key in fixture_member, f"픽스처에 {key} 없음"
        assert key in real_member, f"실백엔드 매핑에 {key} 없음"
