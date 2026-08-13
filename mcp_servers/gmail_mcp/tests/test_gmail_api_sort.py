# mcp_servers/gmail_mcp/tests/test_gmail_api_sort.py
"""gmail_api.search_messages 최신순 정렬 회귀 테스트.

★ 배경 (2026-08-13)
  "내 가장 최근 메일 뭐야?" 요청에 에이전트가 발신자부터 되묻는 버그가 있었다.
  원인 중 하나 — search_messages() 가 Gmail messages.list 응답 순서를 그대로
  돌려주고 있었는데, 그 순서가 실제로 최신순임을 보장하는 공식 계약이 아니다.
  Date 헤더를 직접 파싱해 내림차순으로 재정렬하도록 고쳤고, 이 테스트가 그
  회귀를 막는다 — 목록 API가 뒤섞인 순서로 줘도 최종 결과는 항상 최신순이어야
  한다.
"""
from __future__ import annotations

import pytest

from mcp_servers.gmail_mcp import gmail_api


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def _detail(msg_id: str, date: str, sender: str) -> dict:
    return {
        "id": msg_id,
        "threadId": f"t-{msg_id}",
        "snippet": f"snippet-{msg_id}",
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": f"subject-{msg_id}"},
                {"name": "Date", "value": date},
            ]
        },
    }


class _FakeAsyncClient:
    """httpx.AsyncClient 대역. URL로 목록 조회 / 상세 조회를 구분해 응답한다."""

    def __init__(self, list_ids: list[str], details: dict[str, dict]):
        self._list_ids = list_ids
        self._details = details

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None, params=None):
        if url.endswith("/messages"):
            return _FakeResponse({"messages": [{"id": mid} for mid in self._list_ids]})
        # /messages/{id} 상세 조회
        msg_id = url.rsplit("/", 1)[-1]
        return _FakeResponse(self._details[msg_id])


def _patch(monkeypatch, list_ids: list[str], details: dict[str, dict]):
    def _factory(*args, **kwargs):
        return _FakeAsyncClient(list_ids, details)

    monkeypatch.setattr(gmail_api.httpx, "AsyncClient", _factory)


async def test_뒤섞인_응답_순서를_최신순으로_재정렬한다(monkeypatch):
    # Gmail 목록 API가 뒤섞인(오래된 게 먼저인) 순서로 돌려준다고 가정한다.
    details = {
        "1": _detail("1", "Mon, 10 Aug 2026 09:00:00 +0900", "임다혜 <ida@example.com>"),
        "2": _detail("2", "Wed, 12 Aug 2026 18:30:00 +0900", "조현아 <hjo@example.com>"),
        "3": _detail("3", "Tue, 11 Aug 2026 12:00:00 +0900", "이상훈 <slee@example.com>"),
    }
    _patch(monkeypatch, list_ids=["1", "2", "3"], details=details)

    results = await gmail_api.search_messages("token", query="", max_results=10)

    assert [m["id"] for m in results] == ["2", "3", "1"]  # 8/12 → 8/11 → 8/10
    assert results[0]["from"] == "조현아 <hjo@example.com>"


async def test_빈_쿼리도_그대로_요청된다(monkeypatch):
    """query="" ("가장 최근 메일" 같은 무조건 요청)도 유효한 호출이어야 한다."""
    captured: dict = {}

    class _CapturingClient(_FakeAsyncClient):
        async def get(self, url, headers=None, params=None):
            if url.endswith("/messages"):
                captured["params"] = params
            return await super().get(url, headers=headers, params=params)

    def _factory(*args, **kwargs):
        return _CapturingClient(["1"], {"1": _detail("1", "Mon, 10 Aug 2026 09:00:00 +0900", "a@example.com")})

    monkeypatch.setattr(gmail_api.httpx, "AsyncClient", _factory)

    await gmail_api.search_messages("token", query="", max_results=1)

    assert captured["params"]["q"] == ""


async def test_date_헤더가_없거나_파싱_불가하면_맨_뒤로_보낸다(monkeypatch):
    details = {
        "1": _detail("1", "", "no-date@example.com"),  # Date 헤더 없음(빈 값)
        "2": _detail("2", "Wed, 12 Aug 2026 18:30:00 +0900", "조현아 <hjo@example.com>"),
        "3": _detail("3", "not-a-real-date", "broken@example.com"),  # 파싱 불가
    }
    _patch(monkeypatch, list_ids=["1", "2", "3"], details=details)

    results = await gmail_api.search_messages("token", query="", max_results=10)

    # 정상 파싱된 "2"만 맨 앞, 나머지 둘은 뒤로(순서 자체는 안정성 보장 안 해도 됨)
    assert results[0]["id"] == "2"
    assert {m["id"] for m in results[1:]} == {"1", "3"}
