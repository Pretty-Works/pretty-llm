"""state_token 서명/검증 — 신뢰 경계 그 자체라 제일 먼저 테스트해야 하는 부분."""

import time

import pytest

from mcp_servers.gmail_mcp import state_token
from mcp_servers.gmail_mcp.config import get_settings


@pytest.fixture(autouse=True)
def _fixed_secret(monkeypatch):
    monkeypatch.setenv("GMAIL_MCP_STATE_SECRET", "test-secret-please-ignore")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_roundtrip():
    state = state_token.issue("user-42")
    assert state_token.verify(state) == "user-42"


def test_tampered_state_rejected():
    state = state_token.issue("user-42")
    tampered = state[:-1] + ("a" if state[-1] != "a" else "b")
    with pytest.raises(state_token.InvalidState):
        state_token.verify(tampered)


def test_expired_state_rejected(monkeypatch):
    monkeypatch.setenv("GMAIL_MCP_STATE_SECRET", "test-secret-please-ignore")
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "oauth_state_ttl_s", 0)

    state = state_token.issue("user-42")
    time.sleep(1.1)
    with pytest.raises(state_token.InvalidState):
        state_token.verify(state)
