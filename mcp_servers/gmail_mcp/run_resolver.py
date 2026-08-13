# mcp_servers/gmail_mcp/run_resolver.py
"""run_id → user_id 조회. Gmail MCP가 "user_id를 어디서 얻는가"의 유일한 통로.

⑧ 설계 원칙: Agent/LLM은 run_id만 안다(RunContext contextvar로 이미 흐르고 있던 값,
LLM이 직접 채워 넣을 수 없음). Gmail MCP도 user_id를 클라이언트나 LLM에게서 직접
받지 않는다 — 대신 run_id를 받아서 이 모듈을 통해 "이 run이 어느 user_id 것인지"를
Spring BE에 서버 투 서버로 물어본다.

★ 2026-08-12 BE API 확정 — GET /api/internal/agent/runs/{runId}/user.
  dev_run_id_passthrough 는 이제 로컬에서 BE 없이 돌릴 때만 쓰는 임시 우회로다
  (운영 .env 에는 반드시 false).

★ BE 응답도 다른 내부 API들과 똑같이 {errorCode, message, result} 로 한 겹
  감싸서 온다(Spring 쪽 ResponseInterceptor 가 전 응답에 공통 적용) —
  {"errorCode": null, "message": "SUCCESS", "result": {"userId": 123}} 형태.
  app/clients/backend.py(에이전트 쪽)의 _unwrap() 이 이미 이 봉투를 벗기고
  쓰는 것과 같은 이유로, 여기서도 result 를 먼저 벗긴 뒤에 user_id/userId 를
  찾아야 한다 — 최상위에서 바로 찾으면 항상 못 찾는다(실제로 이 버그로
  Gmail 연동 시도마다 RunResolutionError 가 났었다. _resolve_via_be() 참고).
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
    # ★ BE 공통 응답 봉투 {errorCode, message, result} 를 먼저 벗긴다 — 최상위에서
    #   바로 찾으면 항상 None 이라 매번 아래 에러로 떨어졌다(2026-08-12 확인된 버그).
    #   result 가 아예 없는 응답(옛 스펙·오류 응답 등)도 대비해 data 자체로 폴백한다.
    result = data.get("result")
    if result is None:
        result = data
    # 필드명도 흔한 두 표기 다 받아준다.
    user_id = result.get("user_id") or result.get("userId")
    if not user_id:
        raise RunResolutionError(f"BE 응답에 user_id 없음(run_id={run_id}): {data}")
    return str(user_id)
