# app/tests/test_internal_api_auth.py
"""BE 외 아무나 이 서버를 직접 호출할 수 있던 구멍(인증 미들웨어 부재)을 막은 걸 확인한다.

★ 배경
  app/main.py 에는 CORS 미들웨어만 있고 요청을 검증하는 코드가 전혀 없었다 —
  /api/agent/runs, /api/v1/integrations/gmail/* 등을 이 서버 주소만 알면 누구나
  직접 호출할 수 있었다는 뜻. app/common/auth.py 의 verify_internal_api_key() 가
  그 구멍을 막는다: BE가 보내는 X-Internal-Api-Key 헤더를 settings.internal_api_key
  와 대조한다.

  이 테스트가 확인하는 건 두 가지다.
  ① 배선 — main.py 의 include_router() 들이 실제로 이 의존성을 걸었는가
     (함수 자체는 멀쩡해도 안 걸려 있으면 아무 의미 없다 — 이번 세션에서
     current_run_id/history 가 똑같은 이유로 조용히 안 걸려 있던 전례가 있다).
  ② 함수 로직 — 키 미설정(BE 발급 전 임시 상태)이면 통과, 키 설정 후에는
     정확히 일치할 때만 통과.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.common.auth import verify_internal_api_key


def _routes_under(prefix: str) -> list[APIRoute]:
    from app.main import app

    return [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith(prefix)]


# ★ 원래는 위 _routes_under() + route.dependant.dependencies 정적 순회로
#   "/api/agent" 와 "/api/v1/integrations" 밑 라우트에 인증 의존성이 걸렸는지
#   확인했는데, 이 두 prefix 에서만 매번 빈 리스트가 나와 테스트가 깨졌다
#   (같은 세션에서 test_ai_summary.py 가 /api/agent/project-summary 를 실제로
#   쳐서 401을 받는 걸로 그 라우트가 살아있다는 게 증명되는데도). 원인을
#   못 밝혀 정적 순회 자체를 신뢰하지 않기로 하고, 이 코드베이스가 이미 쓰는
#   방식(test_ai_summary.py, test_meeting_draft.py 등) — 실제로 ASGITransport
#   로 요청을 쏴서 401 이 나오는지/헤더를 주면 뚫리는지 — 로 검증을 바꿨다.
#   /health 하나만 확인하는 test_health_endpoint_stays_open() 은 이미 통과하고
#   있어서 그대로 둔다.
_ENDPOINTS_REQUIRING_AUTH = [
    ("POST", "/api/agent/runs", {}),
    ("GET", "/api/v1/integrations/gmail/status?run_id=1", None),
]


@pytest.mark.parametrize("method,path,body", _ENDPOINTS_REQUIRING_AUTH)
async def test_internal_routes_reject_requests_without_valid_key(method, path, body, monkeypatch):
    """BE 전용이어야 하는 라우터들에 verify_internal_api_key 가 실제로 걸렸는가.

    ★ /api/v1/projects, /api/v1/meetings, /api/v1/vacations, /api/v1/chat 은
    routes.py 에 자리만 있고 아직 엔드포인트가 없다(project.py/meeting.py의
    `router`는 비어 있음 — 실제 로직은 prefix 없는 `agent_router` 쪽에 있다).
    그래서 여기서 검증 대상에서 뺐다 — 라우트가 없는 엔드포인트를 넣으면
    다른 이유(404 등)로 테스트가 의미 없어진다.

    헤더 없이/틀린 키로 401 이 나오는지, 맞는 키를 주면 401 을 벗어나는지만
    본다 — 그 뒤 비즈니스 로직까지 200 으로 성공하는지는 이 테스트의 관심사가
    아니다(예: gmail-mcp 서버가 안 떠 있으면 502/500 이 날 수 있는데, 그건
    인증 게이트를 통과했다는 뜻이라 오히려 이 테스트가 맞다고 보는 결과다)."""
    from app.config import get_settings
    from app.main import app

    monkeypatch.setenv("INTERNAL_API_KEY", "test-secret-for-auth-wiring")
    get_settings.cache_clear()

    # raise_app_exceptions=False: 맞는 키를 준 뒤에는 엔드포인트 본연의 로직이
    # 돈다 — gmail/status 같은 라우트는 실제 gmail-mcp 서버로 나가려다 이 테스트
    # 환경(그 서버가 안 떠 있음)에서 ConnectError 를 던질 수 있다. Starlette의
    # ServerErrorMiddleware 는 등록된 전역 예외 핸들러로 500 응답을 만든 뒤에도
    # 예외 자체를 다시 raise 하므로, 기본값(True)이면 그 예외가 여기까지 그대로
    # 튀어 테스트가 "인증은 통과했는데 그 뒤가 죽었다"를 "테스트 자체가 에러"로
    # 잘못 보고한다. False 로 두면 그 500 응답을 그냥 Response 로 받는다 — 이
    # 테스트는 401 여부만 보므로 그걸로 충분하다.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        no_header = await client.request(method, path, json=body)
        assert no_header.status_code == 401, \
            f"{method} {path} 가 X-Internal-Api-Key 없이도 통과함 (인증 의존성이 안 걸려 있음)"

        wrong_header = await client.request(
            method, path, json=body, headers={"X-Internal-Api-Key": "someone-guessed-this"}
        )
        assert wrong_header.status_code == 401, \
            f"{method} {path} 가 틀린 X-Internal-Api-Key 로도 통과함"

        with_header = await client.request(
            method, path, json=body, headers={"X-Internal-Api-Key": "test-secret-for-auth-wiring"}
        )
        assert with_header.status_code != 401, \
            f"{method} {path} 가 올바른 X-Internal-Api-Key 를 줬는데도 401 (인증 의존성이 안 걸렸거나 로직 문제)"


def test_health_endpoint_stays_open():
    """/health 는 배포·모니터링이 찌르는 엔드포인트라 인증에 걸리면 안 된다."""
    routes = _routes_under("/health")
    assert routes, "/health 라우트를 못 찾음"
    for route in routes:
        calls = [dep.call for dep in route.dependant.dependencies]
        assert verify_internal_api_key not in calls


async def test_passes_when_key_unset(monkeypatch):
    """BE 키 발급 전(임시 상태) — 헤더가 없어도 막지 않는다."""
    from app.config import get_settings

    monkeypatch.setenv("INTERNAL_API_KEY", "")
    get_settings.cache_clear()

    await verify_internal_api_key(x_internal_api_key=None)  # 예외 없이 통과해야 한다


async def test_rejects_missing_or_wrong_key_once_configured(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("INTERNAL_API_KEY", "real-be-secret")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as missing:
        await verify_internal_api_key(x_internal_api_key=None)
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as wrong:
        await verify_internal_api_key(x_internal_api_key="someone-guessed-this")
    assert wrong.value.status_code == 401


async def test_accepts_matching_key_once_configured(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("INTERNAL_API_KEY", "real-be-secret")
    get_settings.cache_clear()

    await verify_internal_api_key(x_internal_api_key="real-be-secret")  # 예외 없이 통과
