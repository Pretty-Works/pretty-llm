# app/tests/test_hitl_gmail_run_id_wiring.py
"""hitl._drive()가 gmail_mcp_client._lock_run_id()로 잠긴 도구까지 이어지는
run_id 배선이 실제로 맞물리는지 end-to-end로 확인한다.

★ 이 테스트가 지키는 회귀
  gmail 도구(_lock_run_id로 감싼 것)는 LangGraph의 RunContext(context=ctx)가
  아니라 app/common/run_context.py의 current_run_id contextvar로 run_id를
  읽는다(engine_b/runner.py가 쓰던 것과 같은 채널). engine_a 경로(도메인
  에이전트 → hitl._drive())는 지금까지 이 contextvar를 아무도 set() 한 적이
  없었다 — 즉 mail 도메인을 실제로 연결하기 전까지는 아무도 눈치 못 챌 조용한
  구멍이었다: gmail 도구가 도메인 에이전트에 묶이는 순간부터 매번
  current_run_id.get() == None 을 보고 {"connected": False, "error":
  "no_active_run"} 만 돌려주게 된다. _drive()에 current_run_id.set(run_id) 한
  줄을 추가해 막았고, 이 테스트는 그 배선이 실제로 이어지는지 잠긴 도구를 직접
  통과시켜 확인한다.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool

from app.clients.gmail_mcp_client import _lock_run_id
from app.common import hitl
from app.common.run_context import current_run_id
from app.tools.registry import RunContext


def _fake_locked_gmail_tool() -> StructuredTool:
    """_lock_run_id()를 통과한 gmail_search_emails 모양의 도구.
    (app/tests/test_gmail_mcp_client_run_id_lock.py의 가짜 도구와 동일한 모양)"""

    async def call_tool(**arguments):
        return (f"검색됨: {arguments.get('query')!r}",
                {"run_id_seen": arguments.get("run_id")})

    raw = StructuredTool(
        name="gmail_search_emails",
        description="Gmail 검색.",
        args_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
                "query": {"type": "string"},
            },
            "required": ["run_id", "query"],
        },
        coroutine=call_tool,
        response_format="content_and_artifact",
    )
    locked = _lock_run_id(raw)
    assert locked is not None
    return locked


class _FakeMessage:
    def __init__(self, content):
        self.type = "ai"
        self.content = content
        self.text = content
        self.tool_calls = None


class _FakeAgentCallingGmailTool:
    """astream 도중 실제로 잠긴 gmail 도구를 호출한다 — LangGraph의 ToolNode가
    미들웨어 통과 후 실제 tool.coroutine을 부르는 지점을 최소로 흉내."""

    def __init__(self, locked_tool):
        self._tool = locked_tool
        self.observed_result: str | None = None

    async def astream(self, agent_input, config, context, stream_mode):
        self.observed_result = await self._tool.ainvoke({"query": "in:inbox"})
        yield "updates", {"node": {"messages": [_FakeMessage("done")]}}


async def test_gmail_tool_called_inside_drive_sees_the_real_run_id():
    """_drive()가 세팅한 run_id가 그 안에서 실행되는 gmail 도구까지 전달된다."""
    locked = _fake_locked_gmail_tool()
    agent = _FakeAgentCallingGmailTool(locked)
    ctx = RunContext(run_id="run_e2e_test")

    async for _ in hitl._drive(agent, {"messages": []}, "run_e2e_test", ctx):
        pass

    assert agent.observed_result is not None
    assert "in:inbox" in agent.observed_result   # no_active_run 폴백이 아니라 진짜 호출됨


async def test_drive_sets_run_id_matching_the_argument_not_ctx():
    """run_id 인자와 ctx.run_id가 다른(이론상) 경우에도 _drive()는 run_id 인자를
    쓴다 — stream_command()가 실제로 넘기는 값과 일치시키기 위해서다."""
    locked = _fake_locked_gmail_tool()
    agent = _FakeAgentCallingGmailTool(locked)
    ctx = RunContext(run_id="ctx_run_id_unused_here")

    async for _ in hitl._drive(agent, {"messages": []}, "actual_run_id", ctx):
        pass

    assert agent.observed_result is not None
    assert "in:inbox" in agent.observed_result


async def test_without_drive_wiring_gmail_tool_fails_safe():
    """대조군 — _drive() 밖(=current_run_id 미설정)에서 부르면 안전 폴백으로
    빠진다. _drive()의 set() 이 빠졌을 때 실제로 벌어졌을 상황과 동일하다."""
    locked = _fake_locked_gmail_tool()
    token = current_run_id.set(None)
    try:
        result = await locked.coroutine(query="in:inbox")
    finally:
        current_run_id.reset(token)
    assert result == {"connected": False, "error": "no_active_run"}
