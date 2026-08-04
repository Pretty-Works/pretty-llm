"""
도구 층 스모크 테스트 (mock 백엔드)

가장 중요한 검증은 마지막 하나다 — **승인 시점과 실행 시점의 요청 바디가
바이트 단위로 같은가.** 다르면 백엔드가 AGENT_015 로 거부하며, 명세가
"붙이는 동안 가장 자주 나는 에러"라고 경고한 지점이다.

실행:  uv run python -m app.tests.test_tools
"""

from __future__ import annotations

import asyncio

from langchain.tools import ToolRuntime

from app.clients.backend import canonical_json
from app.tools.meeting_tool import meeting_create, meeting_list
from app.tools.project_tool import project_members, project_search
from app.tools.registry import RunContext, build_request, catalog_name, is_write

ALL_TOOLS = [project_search, project_members, meeting_list, meeting_create]

# LLM 이 만들어낼 법한 인자. followUp 은 선택 항목이지만 null 을 명시한다
# (규격: "null 필드는 포함한다 — 생략과 명시적 null 을 구분")
MEETING_ARGS = {
    "projectId": 3,
    "title": "스프린트 리뷰",
    "meetingDate": "2026-08-05",
    "attendeeIds": [2, 5, 7],
    "purpose": "진행 상황 공유",
    "content": "백엔드 API 68% 완료",
    "followUp": None,
}


def _runtime(ctx: RunContext) -> ToolRuntime:
    """에이전트 없이 도구만 부르기 위한 최소 런타임."""
    return ToolRuntime(
        state={}, context=ctx, config={}, stream_writer=lambda _: None,
        tool_call_id="tc_test", store=None,
    )


def test_runtime_is_hidden_from_llm() -> None:
    """runtime 은 주입값이므로 LLM 에 노출되면 안 된다."""
    for t in ALL_TOOLS:
        props = t.tool_call_schema.model_json_schema().get("properties", {})
        assert "runtime" not in props, f"{t.name} 스키마에 runtime 이 노출됨"


def test_write_tool_args_all_required() -> None:
    """쓰기 도구는 선택 인자를 두지 않는다.

    LLM 이 생략하면 tool_call.args 에서 키가 빠지는데 도구 안에서는 기본값이
    채워져, 승인 params 와 실행 params 가 달라진다.
    """
    schema = meeting_create.tool_call_schema.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"]), "선택 인자가 있음"


def test_catalog_name_mapping() -> None:
    """OpenAI 함수명에는 점을 못 쓰므로 LangChain 이름과 카탈로그 이름을 나눈다."""
    assert is_write("meeting_create")
    assert catalog_name("meeting_create") == "meeting.create"
    assert not is_write("project_search")


async def test_read_tools_call_backend() -> None:
    rt = _runtime(RunContext(run_id="run_test"))

    out = await project_search.coroutine(keyword="그룹웨어", runtime=rt)
    assert "[3]" in out and "그룹웨어 AI 고도화" in out, out

    out = await project_members.coroutine(projectId=3, runtime=rt)
    assert "[5] 이하늘" in out, out

    out = await meeting_list.coroutine(projectId=3, runtime=rt)
    assert "[41]" in out, out


async def test_params_bytes_identical(monkeypatched: dict) -> None:
    """★ 핵심 — 승인 시점 바이트 == 실행 시점 바이트.

    승인 경로: SSE 계층이 tool_call.args 를 build_request 로 변환해 방출
    실행 경로: 도구가 자기 인자를 build_request 로 변환해 전송
    둘이 같은 함수를 쓰므로 결과가 같아야 한다.
    """
    # 승인 경로 — SSE 계층이 만들 바이트
    _, path, params = build_request("meeting_create", MEETING_ARGS)
    approval_bytes = canonical_json(params)
    assert path == "/projects/3/meetings", path

    # 실행 경로 — 도구가 실제로 보낸 바이트를 가로채 비교
    rt = _runtime(RunContext(run_id="run_test", approval_token="apv_test"))
    out = await meeting_create.coroutine(**MEETING_ARGS, runtime=rt)

    sent = monkeypatched["body"]
    assert sent == approval_bytes, (
        f"바이트 불일치 → AGENT_015\n  승인: {approval_bytes!r}\n  실행: {sent!r}"
    )
    assert monkeypatched["approval_token"] == "apv_test", "승인 토큰이 안 실림"
    assert "meetingId=57" in out, out


async def test_params_canonical_takes_priority(monkeypatched: dict) -> None:
    """BE 가 paramsCanonical 을 주면 우리가 만든 것 대신 그 바이트를 그대로 쓴다."""
    given = b'{"from":"backend"}'
    rt = _runtime(RunContext(
        run_id="run_test", approval_token="apv_test", params_canonical=given,
    ))
    await meeting_create.coroutine(**MEETING_ARGS, runtime=rt)
    assert monkeypatched["body"] == given, "paramsCanonical 이 무시됨"


async def main() -> None:
    from app.clients import backend as backend_mod

    # 도구가 실제로 보낸 바이트를 잡아두기 위해 write 를 감싼다
    captured: dict = {}
    original = backend_mod.backend.write

    async def spy(method, path, run_id, approval_token, body):
        captured.update(
            method=method, path=path, run_id=run_id,
            approval_token=approval_token, body=body,
        )
        return await original(method, path, run_id=run_id,
                              approval_token=approval_token, body=body)

    backend_mod.backend.write = spy  # type: ignore[method-assign]

    test_runtime_is_hidden_from_llm();      print("✅ runtime 이 LLM 스키마에서 제외됨")
    test_write_tool_args_all_required();    print("✅ 쓰기 도구 인자가 전부 required")
    test_catalog_name_mapping();            print("✅ 카탈로그 이름 매핑")
    await test_read_tools_call_backend();   print("✅ 조회 도구 3개 호출")
    await test_params_bytes_identical(captured)
    print("✅ 승인 바이트 == 실행 바이트")
    print(f"   {captured['body'].decode()}")
    await test_params_canonical_takes_priority(captured)
    print("✅ paramsCanonical 우선 적용")

    print("\n전부 통과")


if __name__ == "__main__":
    asyncio.run(main())
