# mcp_servers/gmail_mcp/credential_store.py
"""사용자별 Gmail OAuth 크리덴셜 저장소.

★ 이 모듈이 "MCP Server의 안전한 credential storage"에 해당한다.
  - PK는 Company Copilot 의 user_id (Google user_id 아님) — Agent/BE 와 같은 식별자를 써야
    "이 user_id로 검색해줘" 요청이 들어왔을 때 누구 메일함인지 헷갈리지 않는다.
  - refresh_token/access_token 은 crypto.encrypt() 로 감싼 뒤에만 저장한다.
  - Agent 프로세스는 이 DB 파일에 접근할 이유가 전혀 없다 (별도 볼륨/컨테이너 권장).

동시성: sqlite3 자체가 스레드 세이프하지 않아 매 호출마다 커넥션을 새로 열고 닫는다.
트래픽이 커지면 Postgres 등으로 교체 — 인터페이스(get/save/delete)만 유지하면 됨.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from mcp_servers.gmail_mcp import crypto
from mcp_servers.gmail_mcp.config import get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_credentials (
    user_id         TEXT PRIMARY KEY,   -- Company Copilot user_id
    google_email    TEXT NOT NULL,
    access_token    TEXT NOT NULL,      -- 암호화된 값
    refresh_token   TEXT NOT NULL,      -- 암호화된 값
    access_expiry   REAL NOT NULL,      -- epoch seconds
    scopes          TEXT NOT NULL,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
"""


@dataclass
class GmailCredential:
    user_id: str
    google_email: str
    access_token: str   # 평문 (호출부에서만 잠깐 존재)
    refresh_token: str  # 평문
    access_expiry: float
    scopes: str

    @property
    def is_access_token_valid(self) -> bool:
        # 60초 여유를 둬서 "막 만료됐는데 유효하다고 착각" 하는 경계 케이스를 피한다.
        return time.time() < self.access_expiry - 60


def _connect() -> sqlite3.Connection:
    db_path = Path(get_settings().credential_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(_SCHEMA)
    return conn


def _save_sync(cred: GmailCredential) -> None:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO gmail_credentials
                (user_id, google_email, access_token, refresh_token, access_expiry, scopes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                google_email=excluded.google_email,
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                access_expiry=excluded.access_expiry,
                scopes=excluded.scopes,
                updated_at=excluded.updated_at
            """,
            (
                cred.user_id,
                cred.google_email,
                crypto.encrypt(cred.access_token),
                crypto.encrypt(cred.refresh_token),
                cred.access_expiry,
                cred.scopes,
                now,
                now,
            ),
        )


def _get_sync(user_id: str) -> GmailCredential | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id, google_email, access_token, refresh_token, access_expiry, scopes "
            "FROM gmail_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    user_id, google_email, access_enc, refresh_enc, access_expiry, scopes = row
    return GmailCredential(
        user_id=user_id,
        google_email=google_email,
        access_token=crypto.decrypt(access_enc),
        refresh_token=crypto.decrypt(refresh_enc),
        access_expiry=access_expiry,
        scopes=scopes,
    )


def _update_access_token_sync(user_id: str, access_token: str, access_expiry: float) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE gmail_credentials SET access_token = ?, access_expiry = ?, updated_at = ? WHERE user_id = ?",
            (crypto.encrypt(access_token), access_expiry, time.time(), user_id),
        )


def _delete_sync(user_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM gmail_credentials WHERE user_id = ?", (user_id,))


async def save(cred: GmailCredential) -> None:
    await asyncio.to_thread(_save_sync, cred)


async def get(user_id: str) -> GmailCredential | None:
    return await asyncio.to_thread(_get_sync, user_id)


async def update_access_token(user_id: str, access_token: str, access_expiry: float) -> None:
    await asyncio.to_thread(_update_access_token_sync, user_id, access_token, access_expiry)


async def delete(user_id: str) -> None:
    """'연결 해제' — Agent 쪽에도 Google 쪽에도 이 user_id 의 흔적을 안 남긴다."""
    await asyncio.to_thread(_delete_sync, user_id)


async def is_connected(user_id: str) -> bool:
    return await get(user_id) is not None
