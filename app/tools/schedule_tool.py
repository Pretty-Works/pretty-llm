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

# 명세 상한 (docs/tool_specs_write.md — 초과 시 BE 가 REQUEST_001 로 거절한다)
_MAX_PARTICIPANTS = 20


def _reject_bad_participants(participantUserIds: list[int] | None) -> str | None:
    """참가자 목록이 명백히 잘못됐으면 LLM 이 읽을 사유를 돌려준다 (아니면 None).

    ★ 8/13 추가 — schedule_list 는 participantNames(이름)만 주고 userId 는 주지
      않는다(명세 확인). 그래서 LLM 이 참가자 ID 를 알 방법이 구조적으로 없는데,
      실측에서 **존재하지 않는 userId 99개를 지어내** 승인 카드에 "참가자 99명으로
      교체(기존 명단은 대체됩니다)"가 떴다. 승인했으면 기존 참가자가 전부 날아갈
      뻔했다. BE 왕복 전에 여기서 끊고, 무엇을 해야 하는지 문장으로 알려준다.
    """
    if participantUserIds is None:
        return None
    if len(participantUserIds) > _MAX_PARTICIPANTS:
        return (f"참가자를 {len(participantUserIds)}명 지정했는데 최대 "
                f"{_MAX_PARTICIPANTS}명까지만 가능합니다. userId 를 지어내지 마세요 — "
                "schedule_list 는 참가자 '이름'만 주고 userId 는 주지 않습니다. "
                "참가자를 바꾸는 게 아니라면 participantUserIds 를 null 로 두세요"
                "(기존 명단이 그대로 유지됩니다). 정말 바꿔야 하면 project_members "
                "나 user_search 로 이름을 userId 로 변환한 뒤 다시 호출하세요.")
    if any(not isinstance(uid, int) or uid <= 0 for uid in participantUserIds):
        return ("참가자 userId 에 숫자가 아니거나 0 이하인 값이 섞여 있습니다. "
                "project_members 나 user_search 로 실제 userId 를 확인해 다시 호출하세요.")
    return None


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
    key = f"schedule_list:{fromDate}~{toDate}"
    if not r["schedules"]:
        result = f"{fromDate}~{toDate} 에 일정이 없습니다."
        runtime.context.known_facts[key] = result
        return result
    lines = []
    for s in r["schedules"]:
        leave = " [휴가 — schedule_update 로 수정 불가]" if s["isLeave"] else ""
        lines.append(f"- [{s['scheduleId']}] {s['title']} ({s['startAt']}~{s['endAt']}, "
                     f"{s['type']}{leave}, 참가: {', '.join(s['participantNames'])})")
    # ★ 참가자는 '이름'만 온다(명세: participantNames). userId 는 여기서 알 수 없으므로
    #   schedule_update 로 참가자를 바꿀 생각이면 반드시 이름→userId 변환이 먼저다.
    #   이 한 줄이 없으면 LLM 이 userId 를 지어내는 사고가 난다(실측).
    result = (f"일정 {r['totalCount']}건 (참가자는 이름만 표시됨 — userId 가 필요하면 "
              f"project_members·user_search 로 변환할 것):\n" + "\n".join(lines))
    runtime.context.known_facts[key] = result   # analyze_impact 가 재사용 (기간별로 구분 보관)
    return result


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
    participantUserIds: 참가자 userId 목록 (본인 제외, 최대 20명). 혼자면 null.
        ⚠️ userId 를 모르면 지어내지 마라 — project_members·user_search 로 이름을
        변환해 얻은 값만 넣는다. 참가자가 필요 없으면 null.
    """
    ctx = runtime.context
    if (bad := _reject_bad_participants(participantUserIds)):
        return bad
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

    ★ **날짜·시간만 옮기는 경우라면 participantUserIds 는 반드시 null 로 둬라.**
      그래야 기존 참가자가 그대로 유지된다. 일정을 옮기는 것과 참가자를 바꾸는 것은
      서로 다른 일이다 — 사용자가 참가자 얘기를 안 했으면 건드리지 마라.

    ⚠️ participantUserIds 만 규칙이 다르다: null=유지, []=작성자 혼자로 축소,
    값을 주면 **전체 교체**다. 참가자를 "추가"하려면 기존+신규 전체 목록을 보내야
    한다 — 신규만 보내면 기존 참가자가 전부 빠진다. 그런데 schedule_list 는
    참가자 **이름만** 주고 userId 는 주지 않으므로, 먼저 project_members 나
    user_search 로 이름을 userId 로 변환해야 한다. **userId 를 지어내지 마라.**
    휴가 일정(isLeave=true)은 이 도구로 못 고친다 — leave_update 를 쓴다.

    scheduleId: 대상 일정 ID (schedule_list 로 획득)
    """
    ctx = runtime.context
    if (bad := _reject_bad_participants(participantUserIds)):
        return bad
    args = {"scheduleId": scheduleId, "title": title, "startAt": startAt,
            "endAt": endAt, "type": type, "allDay": allDay,
            "participantUserIds": participantUserIds}
    try:
        r = await execute_write("schedule_update", args, ctx)
    except WriteRejectedError as e:
        return str(e)
    return (f"일정 [{scheduleId}] 을 수정했습니다 "
            f"({r['startAt']}~{r['endAt']}, 참가 {r['participantCount']}명).")
