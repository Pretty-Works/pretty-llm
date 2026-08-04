"""
연차 도구 — 잔여(leave.balance) · 목록(leave.list) · 신청(leave.create) · 수정(leave.update)

leave.create/update 는 AUTO_FORBIDDEN(승인자에게 알림) — auto 모드여도 항상 사람 승인.

★ 서버는 잔여 연차 초과를 막지 않는다 (명세 명시). 잔여 2일에 5일을 신청해도
  성공하고 마이너스 연차가 쌓인다. 그래서 신청·기간 연장 전에 반드시
  leave_balance 를 부르고, 응답의 remainingDaysAfter 가 음수면 사용자에게 알린다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend, canonical_json
from app.tools.registry import RunContext, build_request


@tool
async def leave_balance(runtime: ToolRuntime[RunContext]) -> str:
    """요청자의 남은 연차 일수를 조회한다. 신청·수정 전 반드시 먼저 부른다.

    서버는 잔여 초과 신청을 막지 않으므로, 이 확인을 건너뛰면 사용자 모르게
    마이너스 연차가 쌓인다. 공가(EXCUSED)는 연차에서 차감되지 않는다.
    """
    r = await backend.get("/leaves/balance", run_id=runtime.context.run_id)
    return (f"{r['year']}년 연차: 부여 {r['grantedDays']}일 · 사용 {r['usedDays']}일 · "
            f"잔여 {r['remainingDays']}일.")


@tool
async def leave_list(fromDate: str, toDate: str,
                     runtime: ToolRuntime[RunContext]) -> str:
    """기간 내 휴가 내역을 조회한다 (최대 366일). leaveId 를 얻는 유일한 방법.

    "나 언제 휴가 썼더라?" 류 질문과, 휴가 수정 전 대상 특정에 쓴다.
    남의 휴가는 사유(reason)가 null 로 가려져 온다 — 민감 정보라 답변에 싣지 마라.

    fromDate: 시작일 (YYYY-MM-DD)
    toDate:   종료일 (YYYY-MM-DD)
    """
    r = await backend.get("/leaves", run_id=runtime.context.run_id,
                          **{"from": fromDate, "to": toDate})
    if not r["leaves"]:
        return f"{fromDate}~{toDate} 에 휴가 내역이 없습니다."
    kind = {"ANNUAL": "연차", "EXCUSED": "공가"}
    lines = [f"- [{lv['leaveId']}] {lv['userName']} · {kind.get(lv['leaveType'], lv['leaveType'])} "
             f"{lv['startDate']}~{lv['endDate']} ({lv['days']}일)"
             + ("" if lv["canEdit"] else " (타인)") for lv in r["leaves"]]
    return f"휴가 {r['totalCount']}건:\n" + "\n".join(lines)


@tool
async def leave_create(
    leaveType: str,
    startDate: str,
    endDate: str,
    reason: str | None,
    runtime: ToolRuntime[RunContext],
) -> str:
    """연차·공가를 신청한다. 신청 전 사용자 승인을 받는다 (항상 사람 승인).

    신청 전에 leave_balance 로 잔여를 확인하라. 본인 것만 신청할 수 있다 —
    타인 휴가 요청은 "본인만 신청할 수 있어요"라고 거절하라.
    하루짜리면 startDate 와 endDate 를 같은 날짜로.
    선택 항목(reason)도 반드시 값을 넘긴다 — 없으면 null 을 명시한다.

    leaveType: ANNUAL(연차, 차감) | EXCUSED(공가, 차감 없음)
    startDate: 시작일 (YYYY-MM-DD)
    endDate:   종료일 (startDate 이상)
    reason:    사유 (255자). 없으면 null
    """
    ctx = runtime.context
    args = {"leaveType": leaveType, "startDate": startDate,
            "endDate": endDate, "reason": reason}
    method, path, params = build_request("leave_create", args)
    body = ctx.params_canonical or canonical_json(params)
    r = await backend.write(method, path, run_id=ctx.run_id,
                            approval_token=ctx.approval_token, body=body)
    warn = (f" ⚠️ 신청 후 잔여가 {r['remainingDaysAfter']}일로 음수입니다 — 사용자에게 알리세요."
            if r["remainingDaysAfter"] < 0 else "")
    return (f"휴가 신청이 접수되었습니다 ({r['startDate']}~{r['endDate']}, {r['days']}일, "
            f"leaveId={r['leaveId']}, 잔여 {r['remainingDaysAfter']}일).{warn}")


@tool
async def leave_update(
    leaveId: int,
    leaveType: str | None,
    startDate: str | None,
    endDate: str | None,
    reason: str | None,
    runtime: ToolRuntime[RunContext],
) -> str:
    """휴가를 부분 수정한다. 수정 전 사용자 승인을 받는다 (항상 사람 승인).

    바꿀 필드만 값을 넣고 나머지는 null — null 필드는 기존 값이 유지된다.
    예외: reason 은 null=유지, ""(빈 문자열)=사유 지우기.
    기간을 늘리는 방향이면 leave_balance 로 잔여를 먼저 확인하라.
    휴가 "취소"는 이 도구로 못 한다 — 캘린더 화면 안내(navigate)로 처리하라.

    leaveId: 대상 휴가 ID (leave_list 로 획득)
    """
    ctx = runtime.context
    args = {"leaveId": leaveId, "leaveType": leaveType, "startDate": startDate,
            "endDate": endDate, "reason": reason}
    method, path, params = build_request("leave_update", args)
    body = ctx.params_canonical or canonical_json(params)
    r = await backend.write(method, path, run_id=ctx.run_id,
                            approval_token=ctx.approval_token, body=body)
    warn = (f" ⚠️ 수정 후 잔여가 {r['remainingDaysAfter']}일로 음수입니다."
            if r["remainingDaysAfter"] < 0 else "")
    return (f"휴가 [{leaveId}] 를 수정했습니다 ({r['startDate']}~{r['endDate']}, "
            f"{r['days']}일, 잔여 {r['remainingDaysAfter']}일).{warn}")
