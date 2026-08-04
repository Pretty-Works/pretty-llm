"""
마일스톤 도구 — 조회(milestone.list) · 완료 토글(milestone.toggleStatus)

토글은 오너·PM 만 가능하고(PROJECT_005), 마일스톤은 순서대로 완료가 원칙이라
건너뛰면 거부될 수 있다. isNext(다음 차례) 판단은 서버가 해주니 그걸 쓴다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend, canonical_json
from app.tools.registry import RunContext, build_request


@tool
async def milestone_list(projectId: int, runtime: ToolRuntime[RunContext]) -> str:
    """프로젝트의 마일스톤과 완료 현황을 조회한다.

    "다음 마일스톤 언제야?" 류 질문과, 완료 토글 전 대상 특정에 쓴다.
    isOverdue(지연)·isNext(다음 차례)는 서버 계산값이니 직접 날짜 비교하지 마라.
    """
    r = await backend.get(f"/projects/{projectId}/milestones",
                          run_id=runtime.context.run_id)
    if not r["milestones"]:
        return "등록된 마일스톤이 없습니다."
    lines = []
    for m in r["milestones"]:
        mark = "✔" if m["completed"] else ("→ 다음 차례" if m["isNext"] else "□")
        late = " ⚠️지연" if m["isOverdue"] else ""
        lines.append(f"- [{m['milestoneId']}] {mark} {m['goal']} (목표 {m['targetDate']}{late})")
    s = r["summary"]
    return (f"마일스톤 {s['total']}개 중 {s['completed']}개 완료 "
            f"({s['completionRate']}%):\n" + "\n".join(lines))


@tool
async def milestone_toggle_status(projectId: int, milestoneId: int, completed: bool,
                                  runtime: ToolRuntime[RunContext]) -> str:
    """마일스톤을 완료/미완료로 바꾼다. 변경 전 사용자 승인을 받는다.

    오너 또는 PM 만 가능하다 (project_search 의 isOwner·myRole 로 미리 확인).
    마일스톤은 순서대로 완료해야 한다 — milestone_list 의 isNext 가 붙은 것부터.

    projectId:   프로젝트 ID
    milestoneId: 대상 마일스톤 ID (milestone_list 로 획득)
    completed:   목표 상태 (true=완료)
    """
    ctx = runtime.context
    args = {"projectId": projectId, "milestoneId": milestoneId, "completed": completed}
    method, path, params = build_request("milestone_toggle_status", args)
    body = ctx.params_canonical or canonical_json(params)
    r = await backend.write(method, path, run_id=ctx.run_id,
                            approval_token=ctx.approval_token, body=body)
    if not r.get("changed", True):
        return f"마일스톤 [{milestoneId}] 은 이미 그 상태였습니다."
    state = "완료" if r["completed"] else "미완료"
    return (f"마일스톤 '{r['goal']}' 을 {state}로 바꿨습니다 "
            f"(프로젝트 완료율 {r['completionRateAfter']}%).")
