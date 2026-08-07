# mcp_servers/gmail_mcp/google_oauth.py
"""Google OAuth2 code exchange. 다이어그램의 ③~⑥ 구간.

google-auth-oauthlib 없이 httpx로 직접 두 엔드포인트만 친다 — 이 서버가 하는 일은
'authorization code → token' 교환과 'refresh_token → 새 access_token' 갱신, 딱 둘뿐이라
의존성을 늘릴 이유가 없다.
"""

from __future__ import annotations

import time
import urllib.parse

import httpx

from mcp_servers.gmail_mcp.config import get_settings

_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"


class OAuthExchangeError(RuntimeError):
    pass


def build_authorize_url(state: str) -> str:
    """③ Google OAuth 화면으로 사용자를 보낼 URL."""
    settings = get_settings()
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": settings.gmail_scopes,
        "access_type": "offline",   # refresh_token 을 받으려면 필수
        "prompt": "consent",        # 매번 refresh_token 을 재발급받아 회전시킨다
        "state": state,
    }
    return f"{_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"


async def exchange_code_for_tokens(code: str) -> dict:
    """⑤→⑥ authorization code 를 access/refresh token 으로 교환.

    반환: {"access_token", "refresh_token", "expires_in", "scope", ...} (Google 원본 그대로)
    """
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if resp.status_code != 200:
        raise OAuthExchangeError(f"code exchange 실패: {resp.status_code} {resp.text}")
    return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    """access_token 만료 시 refresh_token 으로 재발급. refresh_token 은 보통 안 바뀐다."""
    settings = get_settings()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _TOKEN_ENDPOINT,
            data={
                "refresh_token": refresh_token,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code != 200:
        raise OAuthExchangeError(f"token refresh 실패: {resp.status_code} {resp.text}")
    return resp.json()


async def fetch_google_email(access_token: str) -> str:
    """저장 레코드에 어떤 구글 계정인지 표시해 두기 위한 부가 조회 (필수 아님)."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            _USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    resp.raise_for_status()
    return resp.json().get("email", "")


def expiry_epoch(expires_in: int) -> float:
    return time.time() + float(expires_in)
