# mcp_servers/gmail_mcp/state_token.py
"""OAuth `state` 파라미터 서명/검증.

흐름의 ②→③ 사이에서 쓰인다: Company Copilot 메인 백엔드(로그인 세션을 아는 쪽)가
"지금 로그인한 사용자는 user_id=42" 라는 사실을 서명해 이 값을 `state` 에 실어
Google로 보낸다. 이 MCP 서버는 세션 쿠키를 볼 필요 없이, 같은 secret 으로 서명만
검증하면 "콜백으로 돌아온 code는 user_id=42 것이다" 를 신뢰할 수 있다.

+ CSRF 방지: state 없이 오는 콜백, 서명이 깨진 콜백, 유효시간 지난 콜백은 전부 거부.
"""

from __future__ import annotations

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from mcp_servers.gmail_mcp.config import get_settings


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().oauth_state_secret
    if not secret:
        raise RuntimeError("GMAIL_MCP_STATE_SECRET 미설정")
    return URLSafeTimedSerializer(secret, salt="gmail-mcp-oauth-state")


def issue(user_id: str) -> str:
    """'Gmail 연결하기' 버튼을 누른 시점에 호출. 반환값을 Google authorize URL의 state 로 붙인다."""
    return _serializer().dumps({"user_id": user_id})


class InvalidState(Exception):
    pass


def verify(state: str) -> str:
    """콜백에서 호출. 유효하면 user_id 를 반환하고, 아니면 InvalidState."""
    try:
        payload = _serializer().loads(state, max_age=get_settings().oauth_state_ttl_s)
    except SignatureExpired as exc:
        raise InvalidState("state 만료 — 연결하기부터 다시 시도") from exc
    except BadSignature as exc:
        raise InvalidState("state 서명 불일치 — 위조 의심") from exc
    user_id = payload.get("user_id")
    if not user_id:
        raise InvalidState("state에 user_id 없음")
    return user_id
