"""
할일 도구 — 조회(task.list) · 추가(task.create, 배치) · 완료 토글(task.toggleStatus)

task_create 가 배치인 이유(카탈로그 §4-3): "액션아이템 3개 등록해줘"를 도구 3번으로
처리하면 승인 카드가 3장 뜬다. 배열로 한 번에 보내면 카드 1장 + 단일 트랜잭션
(한 건이라도 실패하면 전체 롤백)이라 "3개 중 2개만 저장"이 없다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend, canonical_json
from app.tools.registry import RunContext, build_request


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

    if not r["tasks"]:
        return f"{r['weekStart']}~{r['weekEnd']} 에 등록된 할 일이 없습니다."
    lines = []
    for t in r["tasks"]:
        flags = ("✔" if t["completed"] else "□") + (" (지난주 이월)" if t["isCarryOver"] else "")
        proj = f" / {t['projectName']}" if t["projectName"] else " / 개인"
        lines.append(f"- [{t['taskId']}] {flags} {t['content']} (마감 {t['dueDate']}{proj})")
    s = r["summary"]
    return (f"{r['weekStart']}~{r['weekEnd']} 할일 {s['total']}건 "
            f"(완료율 {s['completionRate']}% — 서버 계산값):\n" + "\n".join(lines))


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
    method, path, params = build_request("task_create", {"tasks": tasks})
    body = ctx.params_canonical or canonical_json(params)
    r = await backend.write(method, path, run_id=ctx.run_id,
                            approval_token=ctx.approval_token, body=body)
    ids = ", ".join(str(t["taskId"]) for t in r["tasks"])
    return f"할일 {r['createdCount']}건을 등록했습니다 (taskId: {ids})."


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
    method, path, params = build_request("task_toggle_status",
                                         {"taskId": taskId, "completed": completed})
    body = ctx.params_canonical or canonical_json(params)
    r = await backend.write(method, path, run_id=ctx.run_id,
                            approval_token=ctx.approval_token, body=body)
    if not r.get("changed", True):
        return f"할일 [{taskId}] 은 이미 그 상태였습니다."
    state = "완료" if r["completed"] else "미완료"
    return f"할일 [{taskId}] '{r['content']}' 을 {state}로 바꿨습니다."
