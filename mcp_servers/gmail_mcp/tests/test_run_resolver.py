# mcp_servers/gmail_mcp/tests/test_run_resolver.py
"""run_id → user_id 조회(run_resolver.py) — 2026-08-12 발견된 실제 버그 회귀 테스트.

★ 배경
  BE는 다른 내부 API들과 마찬가지로 이 응답도 {errorCode, message, result} 로
  한 겹 감싸서 준다: {"errorCode": null, "message": "SUCCESS", "result": {"userId": 123}}.
  _resolve_via_be() 가 최상위에서 바로 user_id/userId 를 찾고 있어서, 실제 배포
  환경에서 Gmail 연동을 시도할 때마다 RunResolutionError("BE 응답에 user_id 없음")로
  터졌다 — result 를 먼저 벗기도록 고쳤고, 이 테스트가 그 회귀를 막는다.
"""
from __future__ import annotations

import pytest

from mcp_servers.gmail_mcp import run_resolver
from mcp_servers.gmail_mcp.config import get_settings


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setenv("BACKEND_BASE_URL", "http://be.test")
    monkeypatch.setenv("INTERNAL_API_KEY", "test-key")
    monkeypatch.setenv("GMAIL_MCP_RUN_LOOKUP_PATH_TEMPLATE", "/api/internal/agent/runs/{run_id}/user")
    monkeypatch.setenv("GMAIL_MCP_DEV_RUN_PASSTHROUGH", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeResponse:
    def __init__(self, status_code: int, body: dict, text: str = ""):
        self.status_code = status_code
        self._body = body
        self.text = text or str(body)

    def json(self):
        return self._body


class _FakeAsyncClient:
    """httpx.AsyncClient 대역 — get() 이 미리 정해둔 응답을 돌려준다."""

    def __init__(self, response: _FakeResponse, captured: dict | None = None):
        self._response = response
        self._captured = captured if captured is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
        return self._response


def _patch_client(monkeypatch, response: _FakeResponse, captured: dict | None = None):
    def _factory(*args, **kwargs):
        return _FakeAsyncClient(response, captured)

    monkeypatch.setattr(run_resolver.httpx, "AsyncClient", _factory)


# ─── 실제 발견된 버그 — BE 봉투({errorCode,message,result})를 벗기는지 ──────

async def test_be_봉투를_벗기고_userId를_찾는다(monkeypatch):
    """실제 BE 응답 그대로: {"errorCode": null, "message": "SUCCESS", "result": {"userId": 123}}"""
    response = _FakeResponse(200, {"errorCode": None, "message": "SUCCESS", "result": {"userId": 123}})
    _patch_client(monkeypatch, response)

    user_id = await run_resolver.resolve_user_id("run-abc")

    assert user_id == "123"


async def test_result_안에_user_id_스네이크케이스도_받는다(monkeypatch):
    response = _FakeResponse(200, {"errorCode": None, "message": "SUCCESS", "result": {"user_id": 456}})
    _patch_client(monkeypatch, response)

    assert await run_resolver.resolve_user_id("run-abc") == "456"


async def test_result_이_없으면_최상위에서도_찾아본다(monkeypatch):
    """혹시 모를 구버전/다른 응답 모양에 대한 폴백 — result 키 자체가 없으면
    data 자체를 결과로 취급한다."""
    response = _FakeResponse(200, {"userId": 789})
    _patch_client(monkeypatch, response)

    assert await run_resolver.resolve_user_id("run-abc") == "789"


async def test_result은_있는데_userId가_없으면_에러(monkeypatch):
    response = _FakeResponse(200, {"errorCode": None, "message": "SUCCESS", "result": {}})
    _patch_client(monkeypatch, response)

    with pytest.raises(run_resolver.RunResolutionError, match="user_id 없음"):
        await run_resolver.resolve_user_id("run-abc")


# ─── 기존 상태코드 기반 에러 처리 — 봉투 파싱 바뀌어도 그대로 동작해야 함 ──

async def test_404면_run_없음_에러(monkeypatch):
    response = _FakeResponse(404, {"errorCode": "RUN_NOT_FOUND", "message": "not found", "result": None})
    _patch_client(monkeypatch, response)

    with pytest.raises(run_resolver.RunResolutionError, match="존재하지 않거나 만료"):
        await run_resolver.resolve_user_id("run-missing")


async def test_5xx면_BE_조회_실패_에러(monkeypatch):
    response = _FakeResponse(500, {"errorCode": "INTERNAL_ERROR", "message": "boom", "result": None})
    _patch_client(monkeypatch, response)

    with pytest.raises(run_resolver.RunResolutionError, match="500"):
        await run_resolver.resolve_user_id("run-abc")


# ─── 요청 자체가 맞게 나가는지 — 경로/헤더 회귀 ────────────────────────────

async def test_실제_경로와_헤더로_요청한다(monkeypatch):
    captured: dict = {}
    response = _FakeResponse(200, {"errorCode": None, "message": "SUCCESS", "result": {"userId": 1}})
    _patch_client(monkeypatch, response, captured)

    await run_resolver.resolve_user_id("run-xyz")

    assert captured["url"] == "http://be.test/api/internal/agent/runs/run-xyz/user"
    assert captured["headers"] == {"X-Internal-Api-Key": "test-key"}


# ─── dev passthrough — BE 호출 자체를 안 타는지 ───────────────────────────

async def test_dev_passthrough_켜지면_BE를_안_부른다(monkeypatch):
    monkeypatch.setenv("GMAIL_MCP_DEV_RUN_PASSTHROUGH", "true")
    get_settings.cache_clear()

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("passthrough 모드인데 BE를 호출함")

    monkeypatch.setattr(run_resolver.httpx, "AsyncClient", _should_not_be_called)

    assert await run_resolver.resolve_user_id("run-xyz") == "run-xyz"
