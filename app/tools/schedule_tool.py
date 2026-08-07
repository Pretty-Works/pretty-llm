"""
일정 도구 — 조회(schedule.list) · 추가(schedule.create) · 수정(schedule.update)

서버는 일정 겹침을 막지 않는다 (카탈로그 §4-8) — 겹침 판단은 에이전트 몫이다.
추가·수정 전에 schedule_list 로 그 시간대를 확인하고, 겹치면 그대로 진행하되
승인 카드에서 사용자가 알 수 있게 하라.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend
from app.common.exceptions import WriteRejectedError
from app.tools.registry import RunContext
from app.tools.write_exec import execute_write


@tool
async def schedule_list(fromDate: str, toDate: str,
                        runtime: ToolRuntime[RunContext]) -> str:
    """기간 내 일정을 조회한다 (최대 62일). scheduleId 를 얻는 유일한 방법.

    일정을 추가·수정하기 전 겹침 확인용으로도 먼저 부른다.
    fromDate: 시작일 (YYYY-MM-DD)
    toDate:   종료일 (YYYY-MM-DD)
    """
    r = await backend.get("/schedules", run_id=runtime.context.run_id,
                          **{"from": fromDate, "to": toDate})
    if not r["schedules"]:
        return f"{fromDate}~{toDate} 에 일정이 없습니다."
    lines = []
    for s in r["schedules"]:
        leave = " [휴가 — schedule_update 로 수정 불가]" if s["isLeave"] else ""
        lines.append(f"- [{s['scheduleId']}] {s['title']} ({s['startAt']}~{s['endAt']}, "
                     f"{s['type']}{leave}, 참가: {', '.join(s['participantNames'])})")
    return f"일정 {r['totalCount']}건:\n" + "\n".join(lines)


@tool
async def schedule_create(title: str, startAt: str, endAt: str, type: str,
                          allDay: bool, participantUserIds: list[int] | None,
                          runtime: ToolRuntime[RunContext]) -> str:
    """일정을 추가한다. 추가 전 사용자 승인을 받는다.

    먼저 schedule_list 로 그 시간대의 겹침을 확인하라.
    미래의 회의 약속은 회의록(과거 기록)이 아니라 이 도구로 잡는다.

    title:   일정 제목 (200자)
    startAt: 시작일시 (YYYY-MM-DDTHH:MM:SS)
    endAt:   종료일시 (startAt 이상)
    type:    MEETING | FIELDWORK | PERSONAL — 휴가(LEAVE)는 이 도구로 못 만든다
    allDay:  종일 일정이면 true
    participantUserIds: 참가자 userId 목록 (본인 제외, 최대 20명). 혼자면 null
    """
    ctx = runtime.context
    args = {"title": title, "startAt": startAt, "endAt": endAt, "type": type,
            "allDay": allDay, "participantUserIds": participantUserIds}
    try:
        r = await execute_write("schedule_create", args, ctx)
    except WriteRejectedError as e:
        return str(e)
    return (f"일정 '{title}' 을 잡았습니다 (scheduleId={r['scheduleId']}, "
            f"참가 {r['participantCount']}명).")


@tool
async def schedule_update(scheduleId: int, title: str | None, startAt: str | None,
                          endAt: str | None, type: str | None, allDay: bool | None,
                          participantUserIds: list[int] | None,
                          runtime: ToolRuntime[RunContext]) -> str:
    """일정을 부분 수정한다. 수정 전 사용자 승인을 받는다.

    바꿀 필드만 값을 넣고 나머지는 null — null 필드는 기존 값이 유지된다.
    ⚠️ participantUserIds 만 규칙이 다르다: null=유지, []=작성자 혼자로 축소,
    값을 주면 **전체 교체**다. 참가자를 "추가"하려면 schedule_list 로 현재
    명단을 확인해 기존+신규 전체 목록을 보내야 한다 — 신규만 보내면 기존
    참가자가 전부 빠진다.
    휴가 일정(isLeave=true)은 이 도구로 못 고친다 — leave_update 를 쓴다.

    scheduleId: 대상 일정 ID (schedule_list 로 획득)
    """
    ctx = runtime.context
    args = {"scheduleId": scheduleId, "title": title, "startAt": startAt,
            "endAt": endAt, "type": type, "allDay": allDay,
            "participantUserIds": participantUserIds}
    try:
        r = await execute_write("schedule_update", args, ctx)
    except WriteRejectedError as e:
        return str(e)
    return (f"일정 [{scheduleId}] 을 수정했습니다 "
            f"({r['startAt']}~{r['endAt']}, 참가 {r['participantCount']}명).")
