# app/common/backend_client.py
"""백엔드(Spring) 내부 도구 API 읽기 래퍼 (Engine B 분석용).

Engine B 의 도구들은 이 모듈만 통해 백엔드를 읽는다.
호출에 실패하거나 mock 모드면 `BackendUnavailable` 을 던지고, 각 도구가
`app/tools/demo_data.py` 픽스처로 폴백한다.

담당자 1의 `app/clients/backend.py` 와 역할이 겹친다. 그쪽은 승인 토큰·해시
고정까지 다루는 쓰기 포함 창구이고, 여기는 분석에 필요한 조회 전용이다.
그쪽에 할 일·예산 엔드포인트가 생기면 이 모듈은 걷어내고 흡수한다.
"""

from typing import Any

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger("common.backend_client")


class BackendUnavailable(RuntimeError):
    """백엔드가 없거나 호출에 실패했을 때. 호출부는 픽스처로 폴백한다."""


def is_enabled() -> bool:
    return not get_settings().uses_fixtures


def _timeout(settings) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.backend_connect_timeout_s,
        read=settings.backend_read_timeout_s,
        write=settings.backend_read_timeout_s,
        pool=settings.backend_connect_timeout_s,
    )


async def get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET 요청. 실패는 모두 BackendUnavailable 로 정규화한다."""
    settings = get_settings()
    if settings.uses_fixtures:
        raise BackendUnavailable("MOCK_BACKEND=true 또는 BACKEND_BASE_URL 미설정")

    url = f"{settings.backend_base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if settings.internal_api_key:
        headers["X-Internal-Api-Key"] = settings.internal_api_key

    try:
        async with httpx.AsyncClient(timeout=_timeout(settings)) as client:
            response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.warning("backend GET %s 실패 -> 픽스처 폴백: %s", url, exc)
        raise BackendUnavailable(str(exc)) from exc

    # 내부 규격은 {errorCode, message, result} 봉투를 쓴다. 있으면 벗긴다.
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload
