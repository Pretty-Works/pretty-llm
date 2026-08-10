# mcp_servers/gmail_mcp/gmail_api.py
"""Gmail REST API 얇은 래퍼. access_token 을 받아 그 요청에만 쓴다 (저장 안 함)."""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from typing import Any

import httpx

_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


async def search_messages(access_token: str, query: str, max_results: int = 10) -> list[dict[str, Any]]:
    """Gmail 검색 문법(from:, subject:, is:unread ...) 그대로 지원."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        list_resp = await client.get(
            f"{_BASE}/messages",
            headers=_headers(access_token),
            params={"q": query, "maxResults": max_results},
        )
    list_resp.raise_for_status()
    ids = [m["id"] for m in list_resp.json().get("messages", [])]

    results = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for msg_id in ids:
            resp = await client.get(
                f"{_BASE}/messages/{msg_id}",
                headers=_headers(access_token),
                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
            )
            resp.raise_for_status()
            data = resp.json()
            headers = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
            results.append(
                {
                    "id": data["id"],
                    "threadId": data.get("threadId"),
                    "snippet": data.get("snippet"),
                    "from": headers.get("From"),
                    "subject": headers.get("Subject"),
                    "date": headers.get("Date"),
                }
            )
    return results


async def get_message(access_token: str, message_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{_BASE}/messages/{message_id}",
            headers=_headers(access_token),
            params={"format": "full"},
        )
    resp.raise_for_status()
    return resp.json()


async def send_message(access_token: str, to: str, subject: str, body: str) -> dict[str, Any]:
    mime = MIMEText(body)
    mime["to"] = to
    mime["subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{_BASE}/messages/send",
            headers=_headers(access_token),
            json={"raw": raw},
        )
    resp.raise_for_status()
    return resp.json()
