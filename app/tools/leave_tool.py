"""
연차 도구 — 잔여 조회(READ) · 신청(WRITE)

leave.create 는 AUTO_FORBIDDEN 목록(registry)에 있다 — 승인자에게 알림이
바로 나가는 작업이라, auto 모드가 켜져 있어도 BE 가 사람 승인을 강제한다.
우리 쪽 코드는 그걸 신경 쓸 필요가 없다: 언제나처럼 approval_request 를
내보내면 auto 분기는 BE 몫이다 (A안).
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend, canonical_json
from app.tools.registry import RunContext, build_request


@tool
async def leave_balance(runtime: ToolRuntime[RunContext]) -> str:
    """요청자의 남은 연차 일수를 조회한다.

    "연차 며칠 남았어?" 또는 연차 신청 전에 잔여가 충분한지 확인할 때 부른다.
    """
    r = await backend.get("/leaves/balance", run_id=runtime.context.run_id)
    return (f"{r['year']}년 연차: 총 {r['total']}일 중 {r['used']}일 사용, "
            f"{r['remaining']}일 남음.")


@tool
async def leave_create(
    startDate: str,
    endDate: str,
    leaveType: str,
    reason: str | None,
    runtime: ToolRuntime[RunContext],
) -> str:
    """연차를 신청한다. 신청 전 사용자 승인을 받는다.

    하루짜리면 startDate 와 endDate 를 같은 날짜로 넣는다.
    선택 항목(reason)도 반드시 값을 넘긴다 — 없으면 null 을 명시한다.

    startDate: 시작일 (YYYY-MM-DD)
    endDate:   종료일 (YYYY-MM-DD)
    leaveType: ANNUAL(연차) | HALF_AM(오전 반차) | HALF_PM(오후 반차)
    reason:    사유. 없으면 null
    """
    ctx = runtime.context

    args = {
        "startDate": startDate,
        "endDate": endDate,
        "leaveType": leaveType,
        "reason": reason,
    }
    method, path, params = build_request("leave_create", args)
    body = ctx.params_canonical or canonical_json(params)

    r = await backend.write(
        method, path,
        run_id=ctx.run_id,
        approval_token=ctx.approval_token,
        body=body,
    )
    return f"연차 신청이 접수되었습니다 ({startDate}~{endDate}, leaveId={r['leaveId']})."


READ_TOOLS = [leave_balance]
WRITE_TOOLS = [leave_create]
