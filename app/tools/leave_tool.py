"""
연차 도구 — 잔여(leave.balance) · 목록(leave.list) · 신청(leave.create) · 수정(leave.update)

leave.create/update 는 AUTO_FORBIDDEN(승인자에게 알림) — auto 모드여도 항상 사람 승인.

★ 서버는 잔여 연차 초과를 막지 않는다 (명세 명시). 잔여 2일에 5일을 신청해도
  성공하고 마이너스 연차가 쌓인다. 그래서 신청·기간 연장 전에 반드시
  leave_balance 를 부르고, 응답의 remainingDaysAfter 가 음수면 사용자에게 알린다.

★ 8/12 추가 — LLM 프롬프트 지시(analyze_impact 호출 여부 등)에만 기대지 않고,
  잔여 초과·마감 겹침을 코드로 결정적으로 확인한다. 두 지점에서 쓴다.
  1) 승인 카드가 뜨기 "전" — hitl._approval_payload 가 preview_leave_risks() 를
     불러 "신청" 버튼을 누르기 전에 경고를 보여준다.
  2) leave_create 실행 시점(승인 후, 실제 백엔드 호출 직전) — 그 사이 데이터가
     바뀔 수 있어 같은 확인을 한 번 더 하고, 잔여 초과면 여기서 등록 자체를
     막는다.
"""

from __future__ import annotations

from datetime import date, timedelta

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend
from app.common.exceptions import WriteRejectedError
from app.tools.registry import RunContext
from app.tools.write_exec import execute_write


@tool
async def leave_balance(runtime: ToolRuntime[RunContext]) -> str:
    """요청자의 남은 연차 일수를 조회한다. 신청·수정 전 반드시 먼저 부른다.

    서버는 잔여 초과 신청을 막지 않으므로, 이 확인을 건너뛰면 사용자 모르게
    마이너스 연차가 쌓인다. 공가(EXCUSED)는 연차에서 차감되지 않는다.
    """
    r = await backend.get("/leaves/balance", run_id=runtime.context.run_id)
    result = (f"{r['year']}년 연차: 부여 {r['grantedDays']}일 · 사용 {r['usedDays']}일 · "
              f"잔여 {r['remainingDays']}일.")
    runtime.context.known_facts["leave_balance"] = result   # analyze_impact 가 재사용
    return result


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


# ── 코드 가드 헬퍼 (leave_create 실행부 · 승인 카드 미리보기 양쪽에서 재사용) ──

async def _check_balance(run_id: str, leaveType: str, start: date, end: date) -> str:
    """ANNUAL 신청이 잔여를 넘는지 확인한다. 넘으면 경고 문구, 아니면 빈 문자열."""
    if leaveType != "ANNUAL":
        return ""
    bal = await backend.get("/leaves/balance", run_id=run_id)
    requested_days = (end - start).days + 1
    if requested_days > bal["remainingDays"]:
        return f"잔여 연차 {bal['remainingDays']}일보다 많은 {requested_days}일 신청(초과)"
    return ""


async def _week_offset(run_id: str, target: date) -> int:
    """target 날짜가 속한 주의 weekOffset(오늘 기준, task_list 와 동일 기준)을 구한다."""
    me = await backend.get("/me", run_id=run_id)
    this_week_start = date.fromisoformat(me["thisWeekStart"])
    target_week_start = target - timedelta(days=target.weekday())
    return (target_week_start - this_week_start).days // 7


async def _due_tasks_in_range(run_id: str, start: date, end: date) -> list[dict]:
    """[start, end] 기간에 마감(dueDate)이고 아직 완료되지 않은 내 할일을 모은다.

    task_list 와 같은 /tasks 엔드포인트를 쓴다. weekOffset 범위(-8~8)를 벗어나면
    그 주는 건너뛴다 — 이 검사는 "알려주는" 보조 기능이라 실패해도 신청 자체를
    막지는 않는다(조회 실패는 호출부에서 흡수한다).
    """
    start_offset = await _week_offset(run_id, start)
    end_offset = await _week_offset(run_id, end)
    hits: list[dict] = []
    seen_ids: set[int] = set()
    for offset in range(max(start_offset, -8), min(end_offset, 8) + 1):
        r = await backend.get("/tasks", run_id=run_id, weekOffset=offset)
        for t in r.get("tasks", []):
            if t["completed"] or t["taskId"] in seen_ids:
                continue
            due = date.fromisoformat(t["dueDate"])
            if start <= due <= end:
                hits.append(t)
                seen_ids.add(t["taskId"])
    return hits


async def preview_leave_risks(run_id: str, leaveType: str, startDate: str, endDate: str) -> str:
    """승인 카드용 사전 경고 — "신청" 버튼을 누르기 전에 보여준다.

    app/common/hitl.py 의 _approval_payload 가 leave_create 호출을 가로챌 때
    부른다. leave_create 내부의 코드 가드와 같은 조회를 승인 시점에 한 번 더
    해서 previewText 에 얹는다 — 그래야 사용자가 승인하기 "전에" 잔여 초과나
    마감 겹침을 볼 수 있다(실행 시점 경고는 이미 늦다).
    조회가 실패해도 승인 카드 자체를 막으면 안 되므로 전부 흡수한다.
    """
    start = date.fromisoformat(startDate)
    end = date.fromisoformat(endDate)
    warnings: list[str] = []

    try:
        over = await _check_balance(run_id, leaveType, start, end)
        if over:
            warnings.append(over)
    except Exception:
        pass

    try:
        due_tasks = await _due_tasks_in_range(run_id, start, end)
    except Exception:
        due_tasks = []
    if due_tasks:
        names = ", ".join(t["content"] for t in due_tasks[:3])
        more = f" 외 {len(due_tasks) - 3}건" if len(due_tasks) > 3 else ""
        warnings.append(f"이 기간 마감인 할일 {len(due_tasks)}건 — {names}{more}")

    return " / ".join(warnings)


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

    ★ 승인 카드에도 잔여 초과·마감 겹침 경고가 미리 뜬다(hitl.preview_leave_risks).
      ANNUAL 이고 신청 일수가 잔여를 넘으면 이 도구가 등록 자체를 거부한다
      (승인은 됐어도 실제 백엔드 호출 전에 막는다). 그 기간에 마감인 할일이
      있으면 등록 성공 메시지에도 항상 경고를 덧붙인다.

    leaveType: ANNUAL(연차, 차감) | EXCUSED(공가, 차감 없음)
    startDate: 시작일 (YYYY-MM-DD)
    endDate:   종료일 (startDate 이상)
    reason:    사유 (255자). 없으면 null
    """
    ctx = runtime.context
    start = date.fromisoformat(startDate)
    end = date.fromisoformat(endDate)

    # ★ 코드 가드 1 — 잔여보다 큰 ANNUAL 신청은 여기서 막는다.
    over = await _check_balance(ctx.run_id, leaveType, start, end)
    if over:
        return (
            f"신청을 진행하지 않았습니다 — {over}. 기간을 줄이거나, 잔여를 넘겨서라도 "
            "진행할지 사용자에게 먼저 확인한 뒤 다시 시도하세요."
        )

    args = {"leaveType": leaveType, "startDate": startDate,
            "endDate": endDate, "reason": reason}
    try:
        r = await execute_write("leave_create", args, ctx)
    except WriteRejectedError as e:
        return str(e)

    warn = (f" ⚠️ 신청 후 잔여가 {r['remainingDaysAfter']}일로 음수입니다 — 사용자에게 알리세요."
            if r["remainingDaysAfter"] < 0 else "")

    # ★ 코드 가드 2 — 신청 기간에 마감인 할일이 있으면 항상 덧붙인다.
    try:
        due_tasks = await _due_tasks_in_range(ctx.run_id, start, end)
    except Exception:
        due_tasks = []          # 조회 실패로 신청 자체를 막지 않는다
    task_note = ""
    if due_tasks:
        listed = "; ".join(f"[{t['taskId']}] {t['content']} (마감 {t['dueDate']})"
                           for t in due_tasks)
        task_note = (f"\n⚠️ 이 기간에 마감인 할일 {len(due_tasks)}건이 있습니다 — {listed}. "
                    "미리 처리하거나 담당자에게 인계했는지 사용자에게 확인하세요.")

    return (f"휴가 신청이 접수되었습니다 ({r['startDate']}~{r['endDate']}, {r['days']}일, "
            f"leaveId={r['leaveId']}, 잔여 {r['remainingDaysAfter']}일).{warn}{task_note}")


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
    try:
        r = await execute_write("leave_update", args, ctx)
    except WriteRejectedError as e:
        return str(e)
    warn = (f" ⚠️ 수정 후 잔여가 {r['remainingDaysAfter']}일로 음수입니다."
            if r["remainingDaysAfter"] < 0 else "")
    return (f"휴가 [{leaveId}] 를 수정했습니다 ({r['startDate']}~{r['endDate']}, "
            f"{r['days']}일, 잔여 {r['remainingDaysAfter']}일).{warn}")
