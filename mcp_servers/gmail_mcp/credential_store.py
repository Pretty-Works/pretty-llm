# mcp_servers/gmail_mcp/credential_store.py
"""사용자별 Gmail OAuth 크리덴셜 저장소.

★ 이 모듈이 "MCP Server의 안전한 credential storage"에 해당한다.
  - PK는 Company Copilot 의 user_id (Google user_id 아님) — Agent/BE 와 같은 식별자를 써야
    "이 user_id로 검색해줘" 요청이 들어왔을 때 누구 메일함인지 헷갈리지 않는다.
  - refresh_token/access_token 은 crypto.encrypt() 로 감싼 뒤에만 저장한다.
  - Agent 프로세스는 이 DB 파일에 접근할 이유가 전혀 없다 (별도 볼륨/컨테이너 권장).
  - google_email 과 별개로 google_subject_id(구글 계정의 불변 고유 ID, OIDC의 sub
    클레임)도 같이 들고 있는다. email 은 "연결됨: xxx@company.com" 뱃지 표시용으로만
    쓰고, "이게 정말 같은 구글 계정인가"를 판별해야 할 일이 생기면 subject_id 를
    본다 — email 은 사용자가 나중에 바꿀 수 있어 장기 식별자로는 약하다.

★ user_id ↔ 구글 계정은 엄격하게 1:1이다.
  - 한 user_id 는 항상 credential 행을 최대 1개만 가진다 (PK가 user_id라 구조적으로
    보장됨 — 재연결은 그 1개 행을 덮어쓴다, 새 행이 추가되는 게 아니다).
  - 한 구글 계정(subject_id 또는 email)은 동시에 두 개의 서로 다른 user_id 에
    연결될 수 없다. `_save_sync()`가 저장 전에 애플리케이션 레벨에서 먼저 검사해
    친절한 에러(`AlreadyLinkedError`)를 내고, 동시 요청으로 그 검사를 통과해버리는
    레이스 컨디션에 대비해 DB에도 UNIQUE 인덱스(부분 인덱스, 빈 문자열 제외)를
    걸어 이중으로 막는다. 두 방어선 중 하나만 있으면 동시성 상황에서 뚫릴 수 있어
    같이 둔다.
  - user_id 가 다른 구글 계정으로 "갈아타는" 것(연결 해제 후 다른 계정 재연결)은
    막지 않는다 — 그 시점에 예전 계정 자리는 비고, 동시에 두 user_id 가 같은
    계정을 갖는 순간이 없으면 1:1 불변식은 계속 성립한다.

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
from mcp_servers.gmail_mcp.logger import get_logger

log = get_logger("credential_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gmail_credentials (
    user_id           TEXT PRIMARY KEY,   -- Company Copilot user_id
    google_subject_id TEXT NOT NULL DEFAULT '',  -- Google 계정의 불변 고유 ID (OIDC sub)
    google_email      TEXT NOT NULL,
    access_token      TEXT NOT NULL,      -- 암호화된 값
    refresh_token     TEXT NOT NULL,      -- 암호화된 값
    access_expiry     REAL NOT NULL,      -- epoch seconds
    scopes            TEXT NOT NULL,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);
"""

# 빈 문자열(userinfo 조회 실패로 subject_id/email 을 못 얻은 행)은 유니크 제약에서
# 제외한다 — 안 그러면 조회 실패가 겹친 서로 다른 두 user_id 가 ''끼리 충돌해서
# 저장 자체가 막혀버린다. WHERE 절 덕분에 partial index 라 빈 값은 몇 개든 허용되고,
# 실제 값이 있는 것끼리만 유니크가 걸린다.
_SUBJECT_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_gmail_credentials_subject_unique
ON gmail_credentials(google_subject_id)
WHERE google_subject_id != '';
"""

_EMAIL_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_gmail_credentials_email_unique
ON gmail_credentials(google_email)
WHERE google_email != '';
"""


class AlreadyLinkedError(Exception):
    """이 구글 계정(subject_id 또는 email)이 이미 다른 user_id 에 연결돼 있어 저장을 거부함."""

    def __init__(self, message: str, *, conflicting_user_id: str) -> None:
        super().__init__(message)
        self.conflicting_user_id = conflicting_user_id


@dataclass
class GmailCredential:
    user_id: str
    google_subject_id: str  # OIDC sub. userinfo 조회 실패 시 "" 로 채워질 수 있음
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
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """이미 만들어진 DB에 새 컬럼/인덱스를 보충한다. 매 커넥션마다 도는 가벼운
    조회/DDL이라 트래픽 영향은 미미하다(전부 IF NOT EXISTS라 이미 있으면 즉시 반환).

    ⚠️ 유니크 인덱스 생성이 실패하면(과거 경고-only 버전 시절에 이미 중복 데이터가
    쌓여있는 경우) 서버를 죽이지 않고 에러 로그만 크게 남긴다 — 그 상태로는
    user_id↔구글계정 1:1이 DB 레벨에서는 보장되지 않으니, 로그를 보고 중복 행을
    수동으로 정리한 뒤 재시작해야 인덱스가 걸린다.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(gmail_credentials)")}
    if "google_subject_id" not in cols:
        conn.execute(
            "ALTER TABLE gmail_credentials ADD COLUMN google_subject_id TEXT NOT NULL DEFAULT ''"
        )

    for label, ddl in (
        ("google_subject_id", _SUBJECT_UNIQUE_INDEX),
        ("google_email", _EMAIL_UNIQUE_INDEX),
    ):
        try:
            conn.execute(ddl)
        except sqlite3.IntegrityError as exc:
            log.error(
                "gmail_credentials 에 %s 기준 중복 행이 이미 있어 UNIQUE 인덱스 생성 실패 — "
                "user_id↔구글계정 1:1 제약이 지금 DB 레벨에서는 안 걸려 있음. "
                "%s 기준으로 중복 행을 찾아 수동 정리 후 서버 재시작 필요: %s",
                label, label, exc,
            )


def _find_owner_sync(column: str, value: str, exclude_user_id: str, conn: sqlite3.Connection) -> str | None:
    if not value:
        return None
    row = conn.execute(
        f"SELECT user_id FROM gmail_credentials WHERE {column} = ? AND user_id != ?",
        (value, exclude_user_id),
    ).fetchone()
    return row[0] if row else None


def _save_sync(cred: GmailCredential) -> None:
    now = time.time()
    with _connect() as conn:
        # 저장 전에 먼저 확인해서 친절한 에러를 낸다(DB 유니크 인덱스는 동시 요청
        # 레이스 컨디션 백스톱이지 기본 방어선이 아니다 — sqlite3.IntegrityError는
        # 메시지가 사용자 친화적이지 않다).
        for column, value in (
            ("google_subject_id", cred.google_subject_id),
            ("google_email", cred.google_email),
        ):
            owner = _find_owner_sync(column, value, cred.user_id, conn)
            if owner:
                raise AlreadyLinkedError(
                    f"{column}={value!r} 는 이미 user_id={owner} 에 연결돼 있음 "
                    f"(user_id={cred.user_id} 가 같은 계정으로 연결 시도)",
                    conflicting_user_id=owner,
                )

        try:
            conn.execute(
                """
                INSERT INTO gmail_credentials
                    (user_id, google_subject_id, google_email, access_token, refresh_token, access_expiry, scopes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    google_subject_id=excluded.google_subject_id,
                    google_email=excluded.google_email,
                    access_token=excluded.access_token,
                    refresh_token=excluded.refresh_token,
                    access_expiry=excluded.access_expiry,
                    scopes=excluded.scopes,
                    updated_at=excluded.updated_at
                """,
                (
                    cred.user_id,
                    cred.google_subject_id,
                    cred.google_email,
                    crypto.encrypt(cred.access_token),
                    crypto.encrypt(cred.refresh_token),
                    cred.access_expiry,
                    cred.scopes,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # 위 사전 체크와 이 INSERT 사이에 다른 요청이 끼어든 경우의 백스톱.
            # UNIQUE 인덱스가 실제로 막아준 것 — 누가 이겼는지는 재조회해야 알 수 있으니
            # 여기서는 "확실히 충돌났다"만 알려준다.
            raise AlreadyLinkedError(
                f"동시 연결 요청 충돌로 저장 실패(user_id={cred.user_id}): {exc}",
                conflicting_user_id="unknown(race condition)",
            ) from exc


def _get_sync(user_id: str) -> GmailCredential | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id, google_subject_id, google_email, access_token, refresh_token, access_expiry, scopes "
            "FROM gmail_credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    user_id, google_subject_id, google_email, access_enc, refresh_enc, access_expiry, scopes = row
    return GmailCredential(
        user_id=user_id,
        google_subject_id=google_subject_id,
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
    """저장/갱신. 이 구글 계정(subject_id 또는 email)이 이미 다른 user_id 에
    연결돼 있으면 `AlreadyLinkedError`를 던진다 — 호출부(oauth_routes.py)가
    이걸 잡아서 사용자에게 실패로 안내해야 한다."""
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
