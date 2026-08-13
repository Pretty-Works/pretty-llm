# mcp_servers/gmail_mcp/tests/test_transport_security.py
"""MCP 엔드포인트(/mcp) Host 헤더 허용 목록 회귀 테스트.

★ 배경 (2026-08-13 발견된 버그)
  mcp_tools.py 의 FastMCP(...) 가 host= 를 안 넘기면 SDK가 기본값 "127.0.0.1"로
  판단해 DNS-rebinding 방지용 Host 헤더 허용 목록을 자동으로
  ["127.0.0.1:*", "localhost:*", "[::1]:*"] 로만 좁혀버린다. Docker Compose
  내부망에서 Agent가 이 서버를 "gmail-mcp:8100" Host 헤더로 부르니 항상
  421 Invalid Host header 로 막혔다 — GMAIL_MCP_ALLOWED_HOSTS 로 실제 배포
  호스트명을 명시하도록 고쳤고, 이 테스트가 그 회귀를 막는다.
"""
from __future__ import annotations

import importlib

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware

from mcp_servers.gmail_mcp.config import get_settings


@pytest.fixture(autouse=True)
def _fixed_settings(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "x")
    monkeypatch.setenv("GMAIL_MCP_STATE_SECRET", "x")
    monkeypatch.setenv("GMAIL_MCP_TOKEN_ENCRYPTION_KEY", "x")
    monkeypatch.setenv("INTERNAL_API_KEY", "x")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _reload_mcp_tools():
    """mcp_tools.py 모듈 최상단에서 get_settings() 를 읽어 mcp 객체를 만들기
    때문에, env 를 바꾼 뒤 확인하려면 모듈을 재로딩해야 한다."""
    import mcp_servers.gmail_mcp.mcp_tools as mod

    return importlib.reload(mod)


def test_기본값에_gmail_mcp_호스트명이_허용된다(monkeypatch):
    """docker-compose 서비스명 gmail-mcp:8100 이 기본 허용 목록에 있어야
    Agent 컨테이너에서 오는 요청이 421 로 막히지 않는다."""
    mod = _reload_mcp_tools()

    assert "gmail-mcp:8100" in mod._allowed_hosts

    mw = TransportSecurityMiddleware(mod.mcp.settings.transport_security)
    assert mw._validate_host("gmail-mcp:8100") is True


def test_허용_안된_호스트는_여전히_막힌다(monkeypatch):
    """DNS rebinding 방지 자체를 꺼버린 게 아니라, 알려진 호스트만 추가로
    허용한 것이어야 한다 — 무관한 호스트는 여전히 거부돼야 한다."""
    mod = _reload_mcp_tools()

    mw = TransportSecurityMiddleware(mod.mcp.settings.transport_security)
    assert mw._validate_host("evil.example.com:8100") is False
    assert mod.mcp.settings.transport_security.enable_dns_rebinding_protection is True


def test_GMAIL_MCP_ALLOWED_HOSTS로_다른_배포_호스트명도_추가할_수_있다(monkeypatch):
    monkeypatch.setenv("GMAIL_MCP_ALLOWED_HOSTS", "localhost:8100,my-custom-host:9000")
    get_settings.cache_clear()
    mod = _reload_mcp_tools()

    assert mod._allowed_hosts == ["localhost:8100", "my-custom-host:9000"]
    mw = TransportSecurityMiddleware(mod.mcp.settings.transport_security)
    assert mw._validate_host("my-custom-host:9000") is True
    # 기본값에 있던 gmail-mcp:8100 은 이제 명시적으로 덮어썼으니 더는 없다.
    assert mw._validate_host("gmail-mcp:8100") is False
