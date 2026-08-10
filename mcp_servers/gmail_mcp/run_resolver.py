# mcp_servers/gmail_mcp/run_resolver.py
"""run_id → user_id 조회. Gmail MCP가 "user_id를 어디서 얻는가"의 유일한 통로.

⑧ 설계 원칙: Agent/LLM은 run_id만 안다(RunContext contextvar로 이미 흐르고 있던 값,
LLM이 직접 채워 넣을 수 없음). Gmail MCP도 user_id를 클라이언트나 LLM에게서 직접
받지 않는다 — 대신 run_id를 받아서 이 모듈을 통해 "이 run이 어느 user_id 것인지"를
Spring BE에 서버 투 서버로 물어본다.

★ BE가 아직 `run_id → user_id` API를 안 내려서, 지금은 dev_run_id_passthrough
  플래그로 우회할 수 있게 해뒀다. BE API가 나오면:
    1) .env 의 GMAIL_MCP_DEV_RUN_PASSTHROUGH 를 지우거나 false로.
    2) config.py 의 run_lookup_path_template 를 BE가 확정한 실제 경로로 수정.
    3) 아래 _resolve_via_be() 의 응답 파싱(user_id 필드명)을 BE 스펙에 맞게 조정.
  이 파일 밖(oauth_routes.py, mcp_tools.py)은 손댈 필요가 없다 — 전부 resolve_user_id()
  하나만 호출하기 때문.
"""

from __future__ import annotations

import httpx

from mcp_servers.gmail_mcp.config import get_settings
from mcp_servers.gmail_mcp.logger import get_logger

log = get_logger("run_resolver")


class RunResolutionError(Exception):
    """run_id로 user_id를 못 찾음 — run이 없거나, 만료됐거나, BE 호출 자체가 실패."""


async def resolve_user_id(run_id: str) -> str:
    """run_id → user_id. 실패하면 RunResolutionError."""

    if not run_id:
        raise RunResolutionError("run_id가 비어 있음")

    settings = get_settings()

    if settings.dev_run_id_passthrough:
        log.warning(
            "⚠️ DEV PASSTHROUGH: run_id=%s 를 user_id로 그대로 사용 — "
            "BE의 run_id→user_id API 붙기 전 로컬 테스트 전용. 운영 .env 에서는 반드시 꺼둘 것.",
            run_id,
        )
        return run_id

    if not settings.backend_base_url:
        raise RunResolutionError(
            "BACKEND_BASE_URL 미설정 — run_id→user_id 조회 불가. "
            "BE API 아직이면 GMAIL_MCP_DEV_RUN_PASSTHROUGH=true 로 로컬 테스트할 것."
        )

    return await _resolve_via_be(run_id)


async def _resolve_via_be(run_id: str) -> str:
    settings = get_settings()
    path = settings.run_lookup_path_template.format(run_id=run_id)
    url = f"{settings.backend_base_url.rstrip('/')}{path}"
    headers = {"X-Internal-Api-Key": settings.internal_api_key}

    try:
        async with httpx.AsyncClient(timeout=settings.run_lookup_timeout_s) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise RunResolutionError(f"BE 호출 실패(run_id={run_id}): {exc}") from exc

    if resp.status_code == 404:
        raise RunResolutionError(f"run_id={run_id} 존재하지 않거나 만료됨")
    if resp.status_code != 200:
        raise RunResolutionError(
            f"BE run 조회 실패: {resp.status_code} {resp.text} (run_id={run_id})"
        )

    data = resp.json()
    # BE 스펙 확정되면 필드명 맞춰서 정리 — 우선 흔한 두 표기 다 받아준다.
    user_id = data.get("user_id") or data.get("userId")
    if not user_id:
        raise RunResolutionError(f"BE 응답에 user_id 없음(run_id={run_id}): {data}")
    return str(user_id)
