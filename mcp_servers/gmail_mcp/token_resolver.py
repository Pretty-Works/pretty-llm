# mcp_servers/gmail_mcp/token_resolver.py
""""user_id → 유효한 access_token" — MCP 툴 구현이 이 함수 하나만 부른다.

⑧ "Agent는 토큰을 전혀 모른다" 를 지키는 경계선이 여기다.
MCP 툴 함수(mcp_tools.py)는 이 모듈이 반환한 access_token 을 그 요청 안에서만
쓰고 버린다 — 리턴값에도, 로그에도 남기지 않는다.
"""

from __future__ import annotations

from mcp_servers.gmail_mcp import credential_store, google_oauth


class NotConnected(Exception):
    """이 user_id 는 아직 'Gmail 연결하기' 를 안 했거나 연결을 해제한 상태."""


async def get_valid_access_token(user_id: str) -> str:
    cred = await credential_store.get(user_id)
    if cred is None:
        raise NotConnected(f"user_id={user_id} 는 Gmail 미연결")

    if cred.is_access_token_valid:
        return cred.access_token

    # 만료됨 → refresh_token으로 조용히 갱신. 사용자는 이 과정을 전혀 모른다(재로그인 없음).
    token_data = await google_oauth.refresh_access_token(cred.refresh_token)
    new_access_token = token_data["access_token"]
    new_expiry = google_oauth.expiry_epoch(token_data.get("expires_in", 3600))
    await credential_store.update_access_token(user_id, new_access_token, new_expiry)
    return new_access_token
