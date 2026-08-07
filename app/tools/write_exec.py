"""
쓰기 도구 공통 실행부 — 규격 §4 의 실패 정책이 사는 곳

모든 쓰기 도구는 execute_write() 하나로 내부 API 를 부른다. 여기서:

  · 승인 바이트 그대로 전송 (params_canonical 우선 — AGENT_015 원천 차단)
  · 5xx / 연결 실패  → 같은 토큰으로 최대 3회 재시도 (토큰 TTL 10분 내)
  · AGENT_015       → 보관한 원본 바이트로 1회 재전송 (규격 지시)
  · 4xx (업무 규칙)  → WriteRejectedError — 도구가 이 메시지를 LLM 에게 돌려주면
                       LLM 이 파라미터를 고쳐 다시 호출하고, 미들웨어가 다시 가로채
                       **새 approval_request** 가 나간다 (실행 중단 아님)
  · AGENT_014 (토큰) → 동일하게 메시지로 — 재승인이 필요함을 알린다

도구 쪽 사용법:
    try:
        r = await execute_write("meeting_create", args, ctx)
    except WriteRejectedError as e:
        return str(e)          # LLM 이 읽는다 — 원인·다음 행동 포함
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.clients.backend import backend, canonical_json
from app.common.exceptions import (
    ApprovalRequiredError,
    BackendUnavailableError,
    BackendValidationError,
    ParamsMismatchError,
    WriteRejectedError,
)
from app.tools.registry import RunContext, build_request

_MAX_5XX_RETRIES = 3          # 규격: TTL 내 같은 토큰으로 재시도, 3회 실패 시 중단


async def execute_write(tool_name: str, args: dict, ctx: RunContext) -> dict[str, Any]:
    """쓰기 실행. 성공 시 result dict, 실패 시 WriteRejectedError(LLM용 안내문)."""
    method, path, params = build_request(tool_name, args)
    body = ctx.params_canonical or canonical_json(params)

    retried_hash = False
    last_5xx: Exception | None = None

    for attempt in range(_MAX_5XX_RETRIES):
        try:
            return await backend.write(method, path, run_id=ctx.run_id,
                                       approval_token=ctx.approval_token, body=body)

        except BackendUnavailableError as e:          # 5xx·연결 실패 — 재시도
            last_5xx = e
            if attempt < _MAX_5XX_RETRIES - 1:
                await asyncio.sleep(0.4 * (attempt + 1))

        except ParamsMismatchError as e:              # AGENT_015 — 원본 바이트 재전송 1회
            if retried_hash:
                raise WriteRejectedError(
                    "저장 내용의 검증(해시)이 계속 어긋납니다(AGENT_015). "
                    "같은 내용으로 새로 승인받아야 합니다 — 도구를 다시 호출하세요."
                ) from e
            retried_hash = True                       # body 는 이미 원본 그대로다

        except BackendValidationError as e:           # 4xx 업무 규칙 — LLM 이 고쳐 재시도
            raise WriteRejectedError(
                f"저장이 거부되었습니다({e.backend_code or '4xx'}): {e}. "
                "위 사유에 맞게 파라미터를 고쳐 도구를 다시 호출하세요 — "
                "내용이 바뀌므로 사용자 승인을 새로 받게 됩니다."
            ) from e

        except ApprovalRequiredError as e:            # AGENT_014 — 토큰 없음·만료·소진
            raise WriteRejectedError(
                "승인 토큰이 없거나 만료되었습니다(AGENT_014). "
                "저장하려면 도구를 다시 호출해 사용자 승인을 새로 받아야 합니다."
            ) from e

    raise WriteRejectedError(
        f"백엔드 일시 장애로 {_MAX_5XX_RETRIES}회 재시도했지만 실패했습니다"
        f"({last_5xx}). 잠시 후 다시 시도해 달라고 사용자에게 안내하세요."
    )
