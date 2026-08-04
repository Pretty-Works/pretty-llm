# app/common/backend_client.py
"""백엔드(Spring) REST 호출 래퍼.

tool 들은 이 모듈만 통해 백엔드를 읽는다.

`BACKEND_BASE_URL` 이 비어 있거나 호출이 실패하면 `BackendUnavailable` 을 던지고,
각 tool 이 `app/tools/demo_data.py` 픽스처로 폴백한다.
백엔드가 아직 안 떠 있어도 Engine B 를 끝까지 돌려볼 수 있게 하기 위한 구조다.
"""

from typing import Any

import requests

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger("common.backend_client")


class BackendUnavailable(RuntimeError):
    """백엔드가 없거나 호출에 실패했을 때. 호출부는 픽스처로 폴백한다."""


def is_enabled() -> bool:
    return not get_settings().uses_fixtures


def get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET 요청. 실패는 모두 BackendUnavailable 로 정규화한다."""
    settings = get_settings()
    if settings.uses_fixtures:
        raise BackendUnavailable("MOCK_BACKEND=true 또는 BACKEND_BASE_URL 미설정")

    url = f"{settings.backend_base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if settings.backend_token:
        headers["Authorization"] = f"Bearer {settings.backend_token}"

    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=settings.backend_timeout
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.warning("backend GET %s 실패 -> 픽스처 폴백: %s", url, exc)
        raise BackendUnavailable(str(exc)) from exc

    # 백엔드 공통 응답이 {"data": ...} 로 감싸는 형태면 벗겨준다.
    if isinstance(payload, dict) and "data" in payload and len(payload) <= 3:
        return payload["data"]
    return payload
