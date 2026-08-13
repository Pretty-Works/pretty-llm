"""
할일 도구 — 조회(task.list) · 추가(task.create, 배치) · 완료 토글(task.toggleStatus) ·
수정(task.update, 전체 교체)

task_create 가 배치인 이유(카탈로그 §4-3): "액션아이템 3개 등록해줘"를 도구 3번으로
처리하면 승인 카드가 3장 뜬다. 배열로 한 번에 보내면 카드 1장 + 단일 트랜잭션
(한 건이라도 실패하면 전체 롤백)이라 "3개 중 2개만 저장"이 없다.

★ 8/12 추가 — task_update 는 PUT 전체 교체다(부분 수정 아님). content·projectId·
  dueDate 를 항상 셋 다 실어 보내야 한다. projectId 를 null 로 보내면(생략이 아니라
  명시적으로) 프로젝트 할일이 개인 할일로 바뀐다 — 도구 docstring 에 못박아 뒀다.

★ 8/12 추가 — task_due_within: "오늘 할일 몇 개 완료했지?" / "내일까지 마감인거
  확인해줘" 류 질문에서 완료/미완료 건수를 LLM 이 task_list 표를 보고 눈으로 세게
  하면 종종 틀린다(수동 카운트 실수 — 이월 항목 중복 세기, 날짜 필터를 잘못
  적용하는 경우가 흔하다). 그래서 app/api/project.py 의 "숫자는 전부 코드가
  계산하고 LLM 은 문장만 쓴다" 원칙을 여기도 적용해, [오늘, untilDate] 구간에
  걸리는 할일만 코드로 걸러 건수까지 센 뒤 문자열로 내려준다. untilDate 를
  생략(=오늘)하면 "오늘 할일"과 동일하다. 기간이 주(weekOffset) 경계를 넘어가면
  (예: 오늘이 일요일이고 내일이 다음 주로 넘어가는 경우) 걸리는 주를 전부 모아
  합친다 — leave_tool.py 의 _due_tasks_in_range 와 같은 패턴.
"""

from __future__ import annotations

from datetime import date, timedelta

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend
from app.common.exceptions import WriteRejectedError
from app.tools.registry import RunContext
from app.tools.write_exec import execute_write


@tool
async def task_list(projectId: int | None, weekOffset: int,
                    runtime: ToolRuntime[RunContext]) -> str:
    """할일을 주 단위로 조회한다. taskId 를 얻는 유일한 방법이다.

    projectId:  프로젝트 할일이면 ID, 내 개인+전체 할일이면 null
    weekOffset: 0=이번 주, -1=지난 주, 1=다음 주 (-8~8)
    """
    params = {"weekOffset": weekOffset}
    if projectId is not None:
        params["projectId"] = projectId
    r = await backend.get("/tasks", run_id=runtime.context.run_id, **params)

    key = f"task_list:{projectId}:{weekOffset}"
    if not r["tasks"]:
        result = f"{r['weekStart']}~{r['weekEnd']} 에 등록된 할 일이 없습니다."
        runtime.context.known_facts[key] = result
        return result
    lines = []
    for t in r["tasks"]:
        flags = ("✔" if t["completed"] else "□") + (" (지난주 이월)" if t["isCarryOver"] else "")
        proj = f" / {t['projectName']}" if t["projectName"] else " / 개인"
        # ★ ID를 줄 맨 앞 [123] 형태로 두면 LLM이 답변에 그대로 베끼는 사고가 잦다
        #   (실사용 피드백). 완전히 빼면 task_toggle_status·task_update 호출에 쓸 ID를
        #   못 구하니, "내부용"이라는 티가 나는 형식으로 맨 뒤로 옮긴다.
        lines.append(f"- {flags} {t['content']} (마감 {t['dueDate']}{proj}) "
                     f"[내부관리번호:{t['taskId']}]")
    s = r["summary"]
    # ★ completionRate(%)만 있고 "완료 N건"이 없어서, LLM이 셀 수 있는 값이 없어
    #   직접 세다가 틀리는 사고가 실제로 났다(실사용 피드백: 2건인데 3건이라 답함).
    #   task_due_within 과 같은 패턴으로 코드가 직접 센 값을 준다.
    done = sum(1 for t in r["tasks"] if t["completed"])
    result = (f"{r['weekStart']}~{r['weekEnd']} 할일 {s['total']}건 "
              f"(완료 {done} · 미완료 {s['total'] - done} — 코드로 직접 센 값):\n"
              + "\n".join(lines))
    runtime.context.known_facts[key] = result   # analyze_impact 가 재사용 (주별로 구분 보관)
    return result


def _week_offset(target: date, this_week_start: date) -> int:
    """target 날짜가 속한 주의 weekOffset(오늘 기준, task_list 와 동일 기준)을 구한다."""
    target_week_start = target - timedelta(days=target.weekday())
    return (target_week_start - this_week_start).days // 7


@tool
async def task_due_within(projectId: int | None, untilDate: str | None,
                          runtime: ToolRuntime[RunContext]) -> str:
    """[오늘, untilDate] 구간에 마감인 할일만 모아 정확한 완료/미완료 건수와 함께 보여준다.

    "오늘 할일 뭐 있어?" / "오늘 몇 개 완료했어?" / "내일까지 마감인거 확인해줘"
    / "이번 주까지 마감인거 뭐 있어?" 같은 질문에는 task_list 대신 이 도구를
    써라. 완료/미완료 건수는 이 도구가 코드로 센 값이니 그대로 인용하면 된다 —
    task_list 표를 보고 다시 세지 마라(세다가 자주 틀린다).

    상대 표현("내일"·"이번 주 금요일" 등)은 이 도구가 계산하지 않는다 — 먼저
    user_me 로 오늘을 확인해 절대 날짜(YYYY-MM-DD)로 바꾼 뒤 넘겨라.

    projectId: 특정 프로젝트만 보려면 ID, 전체(개인+프로젝트 전부)면 null
    untilDate: 이 날짜까지(포함, YYYY-MM-DD). "오늘 할일"이면 null(오늘만 조회)
    """
    ctx = runtime.context
    me = await backend.get("/me", run_id=ctx.run_id)
    today_s = me["today"]
    today = date.fromisoformat(today_s)
    this_week_start = date.fromisoformat(me["thisWeekStart"])
    until = date.fromisoformat(untilDate) if untilDate else today
    if until < today:
        return f"untilDate({untilDate})가 오늘({today_s})보다 과거입니다 — 날짜를 다시 확인하세요."

    start_offset = _week_offset(today, this_week_start)
    end_offset = _week_offset(until, this_week_start)

    hits: list[dict] = []
    seen_ids: set[int] = set()
    for offset in range(max(start_offset, -8), min(end_offset, 8) + 1):
        params = {"weekOffset": offset}
        if projectId is not None:
            params["projectId"] = projectId
        r = await backend.get("/tasks", run_id=ctx.run_id, **params)
        for t in r.get("tasks", []):
            if t["taskId"] in seen_ids:
                continue
            due = date.fromisoformat(t["dueDate"])
            if today <= due <= until:
                hits.append(t)
                seen_ids.add(t["taskId"])

    range_label = today_s if until == today else f"{today_s}~{until.isoformat()}"
    key = f"task_due_within:{projectId}:{range_label}"
    if not hits:
        result = f"{range_label} 마감인 할일이 없습니다."
        runtime.context.known_facts[key] = result
        return result

    hits.sort(key=lambda t: t["dueDate"])
    done = [t for t in hits if t["completed"]]
    pending = [t for t in hits if not t["completed"]]
    lines = []
    for t in hits:
        proj = f" / {t['projectName']}" if t["projectName"] else " / 개인"
        lines.append(f"- {'✔' if t['completed'] else '□'} {t['content']}"
                     f" (마감 {t['dueDate']}{proj}) [내부관리번호:{t['taskId']}]")
    result = (f"{range_label} 마감 할일 {len(hits)}건 (완료 {len(done)} · 미완료 {len(pending)} — "
              "코드로 직접 센 값):\n" + "\n".join(lines))
    runtime.context.known_facts[key] = result
    return result


@tool
async def task_create(tasks: list[dict], runtime: ToolRuntime[RunContext]) -> str:
    """할일을 등록한다 (1~10건 배치, 승인 1회). 등록 전 사용자 승인을 받는다.

    여러 건이어도 **반드시 한 번에** 보낸다 — 나눠 부르면 승인 카드가 여러 장 뜬다.
    담당자는 항상 요청자 본인이다. 남의 할일을 만들어 달라면 "본인 할일만
    만들 수 있어요"라고 거절하라.

    tasks: 각 원소는 {"content": 내용(100자), "dueDate": "YYYY-MM-DD",
           "projectId": 프로젝트ID 또는 null(개인 할일)}
           프로젝트 할일의 마감일은 프로젝트 기간 안이어야 한다.
    """
    ctx = runtime.context
    try:
        r = await execute_write("task_create", {"tasks": tasks}, ctx)
    except WriteRejectedError as e:
        return str(e)
    ids = ", ".join(str(t["taskId"]) for t in r["tasks"])
    return f"할일 {r['createdCount']}건을 등록했습니다 (taskId: {ids})."


@tool
async def task_update(taskId: int, content: str, projectId: int | None, dueDate: str,
                      runtime: ToolRuntime[RunContext]) -> str:
    """할일 내용/프로젝트/마감일을 통째로 바꾼다 (PUT, 전체 교체). 변경 전 사용자 승인을 받는다.

    ★ 전체 교체다 — content, projectId, dueDate 를 **항상 셋 다** 보낸다. 하나라도
      기존 값과 다르게(또는 null 로) 보내면 그 필드가 그대로 바뀐다. 특히 projectId
      를 null 로 보내면 프로젝트 할일이 "개인 할일"로 바뀌어 버린다 — 마감일만
      바꾸고 싶어도 기존 content 와 projectId 를 반드시 같이 채워 다시 보내라.

    ★ 기존 값을 모르면 먼저 task_list 로 이 할일의 현재 content/projectId/dueDate 를
      확인한 뒤, 바꾸지 않을 필드는 그 기존 값 그대로 다시 채워서 호출하라. 지어내지
      마라.

    본인이 만든 할일만 수정할 수 있다 (canEdit 확인, task_list 로 미리 확인). 다른
    프로젝트로 옮기거나 프로젝트 할일의 마감일을 바꾸는 경우에만 그 프로젝트 기간
    안인지 검증된다 — project_search 로 먼저 기간을 확인하라.

    taskId:    대상 할일 ID (task_list 로 획득)
    content:   할일 내용 (100자) — 바꾸지 않으면 기존 값 그대로
    projectId: 프로젝트 ID 또는 null(개인 할일) — 바꾸지 않으면 기존 값 그대로
    dueDate:   마감일 (YYYY-MM-DD) — 바꾸지 않으면 기존 값 그대로
    """
    ctx = runtime.context
    args = {"taskId": taskId, "content": content, "projectId": projectId, "dueDate": dueDate}
    try:
        r = await execute_write("task_update", args, ctx)
    except WriteRejectedError as e:
        return str(e)
    proj = f" / {r['projectName']}" if r.get("projectName") else " / 개인"
    return f"할일 '{r['content']}' 을 수정했습니다 (마감 {r['dueDate']}{proj})."


@tool
async def task_toggle_status(taskId: int, completed: bool,
                             runtime: ToolRuntime[RunContext]) -> str:
    """할일을 완료/미완료 상태로 바꾼다. 변경 전 사용자 승인을 받는다.

    "반전"이 아니라 목표 상태를 명시한다 — 완료 처리면 completed=true.
    taskId 는 먼저 task_list 로 찾는다. 비슷한 할일이 여럿이면 임의로 고르지
    말고 되물어라. 본인 할일만 바꿀 수 있다 (canEdit 확인).

    taskId:    대상 할일 ID
    completed: 목표 상태 (true=완료, false=미완료)
    """
    ctx = runtime.context
    try:
        r = await execute_write("task_toggle_status", {"taskId": taskId, "completed": completed}, ctx)
    except WriteRejectedError as e:
        return str(e)
    if not r.get("changed", True):
        return f"할일 '{r['content']}' 은 이미 그 상태였습니다."
    state = "완료" if r["completed"] else "미완료"
    return f"할일 '{r['content']}' 을 {state}로 바꿨습니다."
