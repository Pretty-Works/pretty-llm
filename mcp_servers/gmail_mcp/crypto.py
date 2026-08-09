# mcp_servers/gmail_mcp/crypto.py
"""저장 전 토큰 암호화. — refresh_token 은 사실상 "영구 로그인" 이라 DB 파일이
유출돼도 못 읽게 대칭키(Fernet)로 감싼다. 키는 이 프로세스만 갖는다(Agent는 없음).
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from mcp_servers.gmail_mcp.config import get_settings


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    key = get_settings().token_encryption_key
    if not key:
        raise RuntimeError("GMAIL_MCP_TOKEN_ENCRYPTION_KEY 미설정")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
