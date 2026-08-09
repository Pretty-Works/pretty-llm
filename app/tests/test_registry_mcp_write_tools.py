# app/tests/test_registry_mcp_write_tools.py
"""gmail_send_email 같은 MCP 쓰기 도구가 BE 내부 API 쓰기 도구와 동일하게
승인 게이트(is_write) 대상이 되는지, 그리고 build_request()가 못 다루는
케이스를 is_mcp_write()로 정확히 구분하는지 확인한다.
"""

from __future__ import annotations

import pytest

from app.tools.registry import (
    AUTO_FORBIDDEN,
    MCP_WRITE_TOOLS,
    WRITE_TOOLS,
    build_request,
    catalog_name,
    is_mcp_write,
    is_write,
)


def test_gmail_send_email_requires_approval_like_be_write_tools():
    assert is_write("gmail_send_email") is True
    assert is_mcp_write("gmail_send_email") is True
    assert "gmail_send_email" not in WRITE_TOOLS  # BE 형식(method/path)이 아니어야 함
    assert "gmail_send_email" in MCP_WRITE_TOOLS


def test_read_only_gmail_tools_are_not_write_tools():
    for name in ("gmail_search_emails", "gmail_get_email", "gmail_connection_status"):
        assert is_write(name) is False
        assert is_mcp_write(name) is False


def test_be_write_tools_still_work_exactly_as_before():
    """기존 BE 쓰기 도구 판정/변환 로직에 회귀가 없어야 한다."""
    assert is_write("meeting_create") is True
    assert is_mcp_write("meeting_create") is False
    method, path, params = build_request(
        "meeting_create", {"projectId": 1001, "title": "킥오프"}
    )
    assert method == "POST"
    assert path == "/projects/1001/meetings"
    assert params == {"title": "킥오프"}


def test_build_request_raises_for_mcp_tools_by_design():
    """MCP 쓰기 도구는 method/path가 없어 build_request()를 못 쓴다 — 호출부가
    is_mcp_write()로 먼저 갈라야 한다(app/common/hitl.py의 _approval_payload 참고)."""
    with pytest.raises(KeyError):
        build_request("gmail_send_email", {"to": "a@b.com", "subject": "s", "body": "b"})


def test_catalog_name_maps_gmail_send_and_falls_back_for_unknown():
    assert catalog_name("gmail_send_email") == "gmail.send"
    assert catalog_name("meeting_create") == "meeting.create"
    assert catalog_name("totally_unknown_tool") == "totally_unknown_tool"


def test_gmail_send_registered_in_auto_forbidden():
    assert "gmail.send" in AUTO_FORBIDDEN
