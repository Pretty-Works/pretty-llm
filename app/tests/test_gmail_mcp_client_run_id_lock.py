# app/tests/test_gmail_mcp_client_run_id_lock.py
"""app/clients/gmail_mcp_client.py 의 run_id 잠금(_lock_run_id)이 실제 langchain-core
StructuredTool 실행 경로에서도 동작하는지 검증한다.

★ 진짜 gmail-mcp 서버나 Google 계정 없이도 돌아간다 — langchain_mcp_adapters가
만드는 tool의 "모양"만 그대로 흉내낸 가짜 StructuredTool을 하나 만들어서
`_lock_run_id()`에 통과시키고, `.ainvoke()`로 실제 BaseTool 실행 파이프라인을
태운다. 이게 pyproject.toml 고정 버전(langchain-core==1.5.1,
langchain-mcp-adapters==0.3.0) 소스를 직접 읽어 확인한 두 가지 핵심 전제를
그대로 재현한 것:
  1) args_schema는 pydantic 모델이 아니라 raw JSON schema dict(tool.inputSchema)다.
     dict 스키마는 BaseTool._parse_input()이 pydantic 검증을 안 하고 tool_input을
     그대로 통과시킨다 — 즉 스키마에서 run_id를 지워도 LLM이 tool_call.args에
     run_id를 몰래 끼워 넣으면 그대로 통과된다. 그래서 강제 덮어쓰기가 진짜
     방어선이어야 한다(테스트 3).
  2) MCP tool의 coroutine은 response_format="content_and_artifact"를 전제로
     (content, artifact) 2-tuple을 반환한다. 이 설정을 잃어버리면 tuple이 통째로
     content 취급돼 결과가 깨진다(테스트 2).
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import StructuredTool

from app.clients.gmail_mcp_client import _lock_run_id
from app.common.run_context import current_run_id


def _fake_mcp_gmail_search_tool() -> StructuredTool:
    """langchain_mcp_adapters.tools.convert_mcp_tool_to_langchain_tool() 이
    gmail_search_emails(run_id, query, max_results=10) 에 대해 실제로 만들어내는
    것과 동일한 모양의 StructuredTool — args_schema=raw JSON schema dict,
    coroutine이 (content, artifact) 튜플을 반환, response_format="content_and_artifact"."""

    captured_calls: list[dict[str, Any]] = []

    async def call_tool(**arguments: Any) -> tuple[str, dict]:
        captured_calls.append(arguments)
        return (f"검색 결과: {arguments.get('query')!r}", {"run_id_seen": arguments.get("run_id")})

    tool = StructuredTool(
        name="gmail_search_emails",
        description="Gmail 검색.",
        args_schema={
            "type": "object",
            "title": "gmail_search_emailsArguments",
            "properties": {
                "run_id": {"type": "string", "title": "Run Id"},
                "query": {"type": "string", "title": "Query"},
                "max_results": {"type": "integer", "title": "Max Results", "default": 10},
            },
            "required": ["run_id", "query"],
        },
        coroutine=call_tool,
        response_format="content_and_artifact",
    )
    tool._captured_calls = captured_calls  # type: ignore[attr-defined]  # 테스트 편의용
    return tool


@pytest.fixture(autouse=True)
def _clear_run_id():
    token = current_run_id.set(None)
    yield
    current_run_id.reset(token)


def test_run_id_removed_from_llm_visible_schema():
    locked = _lock_run_id(_fake_mcp_gmail_search_tool())
    assert locked is not None
    assert "run_id" not in locked.args_schema["properties"]
    assert "run_id" not in locked.args_schema["required"]
    # LLM이 실제로 보게 되는 스키마(tool_call_schema)에도 없어야 한다.
    assert "run_id" not in locked.tool_call_schema["properties"]


async def test_response_format_and_tuple_return_preserved():
    """response_format="content_and_artifact" 를 안 챙기면 (content, artifact)
    튜플이 깨진다 — model_copy 대신 처음부터 새 StructuredTool을 만들면 이게 터진다."""
    original = _fake_mcp_gmail_search_tool()
    locked = _lock_run_id(original)
    assert locked is not None
    assert locked.response_format == "content_and_artifact"

    current_run_id.set("run_abc")
    result = await locked.ainvoke({"query": "in:inbox"})
    # response_format이 살아있으면 content만 문자열로 온다(튜플이 그대로 새지 않음).
    assert isinstance(result, str)
    assert "in:inbox" in result


async def test_run_id_is_forced_from_context_not_from_llm():
    """LLM이 tool_call.args에 run_id를 몰래 끼워 넣어도(딕셔너리 스키마라 걸러지지
    않음) 실제로 gmail-mcp에 전달되는 값은 항상 RunContext의 진짜 run_id다."""
    original = _fake_mcp_gmail_search_tool()
    locked = _lock_run_id(original)
    assert locked is not None

    current_run_id.set("real-run-id")
    # LLM이 스키마에 없는데도 run_id를 직접 써서 보낸 경우를 흉내낸다.
    await locked.ainvoke({"query": "hi", "run_id": "attacker-supplied-run-id"})

    calls = original._captured_calls  # type: ignore[attr-defined]
    assert len(calls) == 1
    assert calls[0]["run_id"] == "real-run-id"  # 공격자가 보낸 값이 아니라 진짜 값이 전달됨


async def test_no_active_run_returns_safe_fallback_without_calling_gmail_mcp():
    original = _fake_mcp_gmail_search_tool()
    locked = _lock_run_id(original)
    assert locked is not None

    current_run_id.set(None)  # 대화 세션 밖에서 호출된 상황을 흉내냄
    result = await locked.coroutine(query="hi")

    assert result == {"connected": False, "error": "no_active_run"}
    assert original._captured_calls == []  # gmail-mcp까지 호출이 안 나갔어야 한다


def test_unwrappable_tool_is_dropped_not_exposed_unlocked():
    """coroutine이 없는 tool(예상 못한 형태)은 잠글 수 없으니 노출하지 않는다."""
    broken = StructuredTool(
        name="broken_tool",
        description="sync-only tool, 이 프로젝트 gmail tool 조합에서는 안 나오지만 방어적으로 확인",
        args_schema={"type": "object", "properties": {"run_id": {"type": "string"}}},
        func=lambda **kwargs: "sync result",
    )
    assert _lock_run_id(broken) is None
