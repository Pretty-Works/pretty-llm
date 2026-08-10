# mcp_servers/gmail_mcp/tests/test_credential_store.py
"""user_id ↔ 구글 계정 1:1 제약 검증.

pytest-asyncio 가 asyncio_mode="auto"(pyproject.toml)로 설정돼 있어 별도
마커 없이 async def 테스트가 그대로 돈다.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from mcp_servers.gmail_mcp import crypto
from mcp_servers.gmail_mcp.config import get_settings


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """테스트마다 완전히 새 SQLite 파일 + 새 Fernet 키로 격리한다."""
    monkeypatch.setenv("GMAIL_MCP_CREDENTIAL_DB_PATH", str(tmp_path / "gmail_credentials.sqlite"))
    monkeypatch.setenv("GMAIL_MCP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    crypto._fernet.cache_clear()
    yield
    get_settings.cache_clear()
    crypto._fernet.cache_clear()


def _cred(user_id: str, subject_id: str, email: str):
    from mcp_servers.gmail_mcp.credential_store import GmailCredential

    return GmailCredential(
        user_id=user_id,
        google_subject_id=subject_id,
        google_email=email,
        access_token="access-plain",
        refresh_token="refresh-plain",
        access_expiry=9_999_999_999.0,
        scopes="gmail.readonly",
    )


async def test_same_user_can_reconnect_same_account():
    from mcp_servers.gmail_mcp import credential_store

    await credential_store.save(_cred("user_1", "sub_1", "a@x.com"))
    await credential_store.save(_cred("user_1", "sub_1", "a@x.com"))  # 재연결/갱신, 충돌 아님

    cred = await credential_store.get("user_1")
    assert cred is not None
    assert cred.google_email == "a@x.com"


async def test_user_can_switch_to_a_different_google_account():
    from mcp_servers.gmail_mcp import credential_store

    await credential_store.save(_cred("user_1", "sub_1", "a@x.com"))
    await credential_store.save(_cred("user_1", "sub_2", "b@x.com"))  # 연결 해제 후 재연결 시나리오

    cred = await credential_store.get("user_1")
    assert cred is not None
    assert cred.google_subject_id == "sub_2"
    assert cred.google_email == "b@x.com"


async def test_different_user_same_subject_id_is_rejected():
    from mcp_servers.gmail_mcp import credential_store

    await credential_store.save(_cred("user_1", "sub_1", "a@x.com"))

    with pytest.raises(credential_store.AlreadyLinkedError) as exc_info:
        await credential_store.save(_cred("user_2", "sub_1", "different@x.com"))

    assert exc_info.value.conflicting_user_id == "user_1"
    # 거부됐으니 user_2 에는 아무것도 안 남아야 한다.
    assert await credential_store.get("user_2") is None
    # user_1 의 기존 연결도 그대로 유지돼야 한다(덮어써지면 안 됨).
    cred = await credential_store.get("user_1")
    assert cred.google_email == "a@x.com"


async def test_different_user_same_email_is_rejected():
    from mcp_servers.gmail_mcp import credential_store

    await credential_store.save(_cred("user_1", "sub_1", "a@x.com"))

    with pytest.raises(credential_store.AlreadyLinkedError):
        await credential_store.save(_cred("user_2", "sub_2", "a@x.com"))  # subject_id는 다른데 email만 겹침

    assert await credential_store.get("user_2") is None


async def test_failed_userinfo_lookup_does_not_collide_across_users():
    """userinfo 조회가 실패해서 subject_id/email 이 둘 다 빈 값인 경우가 여러
    user 에 반복돼도(partial unique index가 빈 문자열을 제외하므로) 막히면 안 된다."""
    from mcp_servers.gmail_mcp import credential_store

    await credential_store.save(_cred("user_1", "", ""))
    await credential_store.save(_cred("user_2", "", ""))  # 충돌 아님

    assert await credential_store.is_connected("user_1")
    assert await credential_store.is_connected("user_2")


async def test_disconnect_frees_up_the_email_for_reuse():
    from mcp_servers.gmail_mcp import credential_store

    await credential_store.save(_cred("user_1", "sub_1", "a@x.com"))
    await credential_store.delete("user_1")

    # user_1 이 연결 해제했으니, 다른 user_id가 같은 구글 계정을 연결할 수 있어야 한다.
    await credential_store.save(_cred("user_2", "sub_1", "a@x.com"))
    cred = await credential_store.get("user_2")
    assert cred is not None
    assert cred.google_email == "a@x.com"
