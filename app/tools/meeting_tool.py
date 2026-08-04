"""
회의록 도구 — 조회(READ) · 저장(WRITE)

meeting_create 가 이 프로젝트의 첫 쓰기 도구다. 쓰기 도구는 조회와 두 가지가 다르다:

  ① 미들웨어가 호출 직전에 가로채 approval_request 를 방출하고 실행을 멈춘다.
     사용자(또는 auto 정책) 승인 → BE 가 토큰 발급 → resume 으로 재개된 뒤에야
     이 함수 본문이 돈다.

  ② 요청 바디를 재직렬화하지 않는다.
     BE 가 승인 시점에 params 를 정규화해 SHA-256 을 고정해 뒀다. 우리가 dict 를
     다시 문자열로 만들면 키 순서·유니코드 이스케이프가 미묘하게 달라져 해시가
     어긋나고 AGENT_015 로 거부된다. 그래서
       · BE 가 paramsCanonical 을 주면 그 바이트를 그대로 흘려보내고
       · 없으면 registry.build_request → canonical_json 으로 만든다
     승인 방출 경로도 같은 build_request 를 쓰므로 양쪽이 항상 같은 값이 된다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend, canonical_json
from app.tools.registry import RunContext, build_request


@tool
async def meeting_list(projectId: int, runtime: ToolRuntime[RunContext]) -> str:
    """프로젝트의 회의록 목록을 조회한다.

    "지난주 회의록" 처럼 기존 회의록을 지칭할 때, 대상을 특정하려고 부른다.
    projectId: project_search 로 찾은 프로젝트 ID
    """
    r = await backend.get(
        f"/projects/{projectId}/meetings", run_id=runtime.context.run_id
    )
    items = r.get("meetings", [])
    if not items:
        return f"프로젝트 {projectId} 에 등록된 회의록이 없습니다."

    lines = [f"- [{m['meetingId']}] {m['title']} ({m['meetingDate']})" for m in items]
    return f"회의록 {r.get('totalCount', len(items))}건:\n" + "\n".join(lines)


@tool
async def meeting_create(
    projectId: int,
    title: str,
    meetingDate: str,
    attendeeIds: list[int],
    purpose: str,
    content: str,
    followUp: str | None,
    runtime: ToolRuntime[RunContext],
) -> str:
    """회의록을 저장한다. 저장 전 사용자 승인을 받는다.

    참석자는 이름이 아니라 userId 목록이므로, 먼저 project_members 로 변환해야 한다.
    선택 항목(followUp)도 반드시 값을 넘긴다 — 없으면 null 을 명시한다.

    projectId:   프로젝트 ID
    title:       회의 제목
    meetingDate: 회의 날짜 (YYYY-MM-DD)
    attendeeIds: 참석자 userId 목록
    purpose:     회의 목적
    content:     회의 내용
    followUp:    후속 조치. 없으면 null
    """
    ctx = runtime.context

    args = {
        "projectId": projectId,
        "title": title,
        "meetingDate": meetingDate,
        "attendeeIds": attendeeIds,
        "purpose": purpose,
        "content": content,
        "followUp": followUp,
    }
    method, path, params = build_request("meeting_create", args)

    # BE 가 해시한 바이트가 있으면 그대로. 없으면(mock·폴백) 같은 규칙으로 직렬화.
    body = ctx.params_canonical or canonical_json(params)

    r = await backend.write(
        method, path,
        run_id=ctx.run_id,
        approval_token=ctx.approval_token,
        body=body,
    )
    return f"회의록 '{title}' 을 저장했습니다. (meetingId={r['meetingId']})"


READ_TOOLS = [meeting_list]
WRITE_TOOLS = [meeting_create]
