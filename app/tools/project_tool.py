"""
프로젝트 조회 도구 (READ)

도구 = 내부 API 하나를 LLM 이 부를 수 있게 감싼 껍데기.
판단은 하지 않는다 — 조회해서 LLM 이 읽을 문장으로 돌려줄 뿐이다.

★ docstring 이 곧 LLM 에게 주는 설명서다.
  "무엇을 하는지"뿐 아니라 "언제 쓰는지"까지 적어야 제때 호출한다.

조회 도구는 승인을 받지 않는다 (2026-08-03 규격 변경).
토큰이 없어 백엔드가 승인 여부를 검사할 방법이 없었고, 막지 못하면서 막는 척
물어보는 건 안 물어보는 것보다 나쁘다고 판단해 폐지됐다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend
from app.tools.registry import RunContext


@tool
async def project_search(keyword: str | None, status: str | None,
                         runtime: ToolRuntime[RunContext]) -> str:
    """내가 참여 중인 프로젝트를 찾는다. projectId 를 모를 때 가장 먼저 부른다.

    keyword: 사용자가 **프로젝트 이름을 말했을 때만** 그 일부를 넣는다 (예: "그룹웨어").
      ★ "최근"·"이번"·"내"·"회의" 같은 일반 단어를 넣지 마라 — 이름 부분일치 검색이라
        0건이 된다. 사용자가 프로젝트를 지목하지 않았으면 **null** 로 두면
        참여 중인 전체가 온다.
    status: 보통 null. 그러면 진행 중(ONGOING)만 온다.
      사용자가 "끝난 것까지"·"전부"처럼 완료된 프로젝트도 원하면 "ALL" 을 넣는다.
    """
    params: dict = {}
    if keyword:
        params["keyword"] = keyword
    if status:
        params["status"] = status          # 생략 시 BE 기본값 = ONGOING 만

    r = await backend.get("/projects", run_id=runtime.context.run_id, **params)
    items = r.get("projects", [])
    if not items:
        # 0건은 에러가 아니다(규격) — 다음 행동을 알려줘 LLM 이 스스로 복구하게 한다.
        scope = f"'{keyword}' 로 검색한 " if keyword else ""
        return (f"{scope}참여 중인 프로젝트가 없습니다. "
                f"keyword 를 null 로 두고 다시 부르거나, 끝난 프로젝트까지 보려면 "
                f"status='ALL' 로 불러 보세요.")

    lines = []
    for p in items:
        role = "오너" if p.get("isOwner") else (p.get("myRole") or "참여자")
        locked = "" if p.get("isOpenForContent", True) else " ⚠️쓰기 잠김(완료·보관 프로젝트)"
        lines.append(f"- [{p['projectId']}] {p['name']} · {p['status']} · "
                     f"기간 {p['startDate']}~{p['targetDate']} · 내 역할: {role}{locked}")
    return (f"참여 중인 프로젝트 {r.get('totalCount', len(items))}건 "
            f"(할일·회의·지출 날짜는 이 기간 안이어야 함):\n" + "\n".join(lines))


@tool
async def project_members(projectId: int, runtime: ToolRuntime[RunContext]) -> str:
    """프로젝트 참여자 목록을 조회한다.

    회의록 참석자처럼 사람을 지정해야 할 때 부른다. 사용자는 이름으로 말하지만
    저장에는 userId 가 필요하므로, 이름 → userId 변환에 쓴다.
    projectId: project_search 로 찾은 프로젝트 ID
    """
    r = await backend.get(
        f"/projects/{projectId}/members", run_id=runtime.context.run_id
    )
    items = r.get("members", [])
    if not items:
        return f"프로젝트 {projectId} 의 참여자가 없습니다."

    lines = []
    for m in items:
        me = " ★본인 — 회의록 참석자에 넣지 말 것" if m.get("isMe") else ""
        lines.append(f"- [{m['userId']}] {m['name']} ({m.get('department', '')} · "
                     f"{m.get('position', '')}{', ' + m['role'] if m.get('role') else ''}){me}")
    return f"참여자 {r.get('totalCount', len(items))}명:\n" + "\n".join(lines)


READ_TOOLS = [project_search, project_members]
