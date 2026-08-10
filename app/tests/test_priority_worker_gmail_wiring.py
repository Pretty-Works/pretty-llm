# app/tests/test_priority_worker_gmail_wiring.py
"""엔진B의 priority·risk 워커가 gmail 읽기 도구로 메일을 근거 삼아 분석하되,
승인 게이트 없는 경로로 메일을 "보내"버리진 못하게 막은 걸 확인한다.

★ 배경
  "우선순위 분석해줘, 근데 김대리와의 최근 메일 반영해서" 같은 요청을 처리하려면
  priority 워커가 gmail_search_emails/gmail_get_email 을 스스로 호출할 수 있어야
  한다. risk 워커도 같은 이유로 붙였다(지연·블로커 신호는 이메일에 먼저 드러나는
  경우가 많다) — cost/skill_fit/workload/meeting 은 이메일이 직접 근거가 될 축이
  아니라서 의도적으로 아직 안 붙였다. 근데 이 워커들이 도구를 자율 호출하는
  run_tool_loop()(app/common/
  llm_client.py)에는 engine_a의 build_domain_agent()가 쓰는
  HumanInTheLoopMiddleware 같은 승인 게이트가 없다 — 도구를 부르면 그 즉시
  실행된다. 그러니 gmail_send_email 이 이 경로에 섞이면 분석 중인 LLM이 사람
  승인 없이 실제 메일을 보내버릴 수 있다.

  막은 방식은 이중이다.
  ① app/clients/gmail_mcp_client.py 의 get_gmail_read_tools() — 화이트리스트로
     읽기 3종만 돌려준다(gmail_send_email 만 빼는 블랙리스트가 아니다).
  ② app/common/llm_client.py 의 run_tool_loop() — registry.is_write() 가 True인
     도구가 하나라도 섞여 있으면 그 자체로 거부한다(①이 뚫려도 여기서 잡는다).

  그리고 app/workers/base.py 의 WorkerSpec.async_tools — gmail 도구는 MCP 서버에
  네트워크로 물어봐야 나오는 비동기 값이라, 정적 튜플인 tools 에는 못 넣는다.
  run_worker() 가 실행 직전에 이걸 호출해 tools 와 합친다.
"""

from __future__ import annotations

from datetime import date

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.clients.gmail_mcp_client import get_gmail_read_tools
from app.common import llm_client
from app.schemas.state import AnalysisContext, AnalysisPlan
from app.workers.base import WorkerSpec, run_worker


def _fake_tool(name: str) -> StructuredTool:
    async def call_tool(**kwargs):
        return f"{name} 호출됨"

    return StructuredTool(
        name=name,
        description="테스트용 가짜 도구.",
        args_schema={"type": "object", "properties": {}, "required": []},
        coroutine=call_tool,
    )


# ─── ① get_gmail_read_tools() 화이트리스트 ─────────────────────────

async def test_get_gmail_read_tools_excludes_send(monkeypatch):
    import app.clients.gmail_mcp_client as gmail_client_module

    fakes = [_fake_tool(n) for n in (
        "gmail_search_emails", "gmail_get_email",
        "gmail_send_email", "gmail_connection_status",
    )]

    async def fake_get_gmail_tools():
        return fakes

    monkeypatch.setattr(gmail_client_module, "get_gmail_tools", fake_get_gmail_tools)

    read_tools = await get_gmail_read_tools()
    names = {t.name for t in read_tools}

    assert names == {"gmail_search_emails", "gmail_get_email", "gmail_connection_status"}
    assert "gmail_send_email" not in names


# ─── ② run_tool_loop() 백스톱 ───────────────────────────────────────

async def test_run_tool_loop_rejects_gmail_send_email():
    with pytest.raises(RuntimeError, match="쓰기 도구"):
        await llm_client.run_tool_loop([], [_fake_tool("gmail_send_email")])


async def test_run_tool_loop_rejects_any_registry_write_tool():
    """gmail 전용 검사가 아니라 registry.is_write() 전체를 본다 — BE 쓰기
    도구(meeting_create)로도 똑같이 막히는지 확인."""
    with pytest.raises(RuntimeError, match="쓰기 도구"):
        await llm_client.run_tool_loop([], [_fake_tool("meeting_create")])


async def test_run_tool_loop_allows_read_only_tool_past_the_guard():
    """읽기 도구는 그 검사에 안 걸려야 한다 — max_tool_calls=0 으로 LLM 호출
    직전에 바로 리턴시켜서, API 키 없이도 '가드를 통과했는지'만 확인한다."""
    result = await llm_client.run_tool_loop(
        [], [_fake_tool("gmail_search_emails")], max_tool_calls=0
    )
    assert result.messages == []  # 예외 없이 빈 결과로 조용히 리턴


# ─── 워커 배선 (priority, risk) ──────────────────────────────────────
#
# ★ cost(project)/skill_fit·workload(hcm)/meeting 어댑터는 의도적으로 안 붙였다
#   — 이메일이 직접적인 근거가 될 축이 아니라서, 붙여봤자 도구 스키마만 늘고
#   워커가 잘못 판단해 관련 없는 이메일을 뒤질 가능성만 약간 는다. 필요해지면
#   priority/risk 와 완전히 같은 패턴(async_tools=get_gmail_read_tools 한 줄 +
#   프롬프트 안내)으로 그때 추가하면 된다.

def test_priority_worker_wired_with_gmail_read_tools():
    from app.workers.project.priority import SPEC

    assert SPEC.async_tools is get_gmail_read_tools


def test_priority_prompt_explains_when_to_use_gmail():
    from app.prompts.priority import METHOD

    assert "gmail_search_emails" in METHOD
    assert "gmail_connection_status" in METHOD


def test_risk_worker_wired_with_gmail_read_tools():
    from app.workers.project.risk import SPEC

    assert SPEC.async_tools is get_gmail_read_tools


def test_risk_prompt_explains_when_to_use_gmail():
    from app.prompts.risk import METHOD

    assert "gmail_search_emails" in METHOD
    assert "gmail_connection_status" in METHOD


def test_other_project_and_domain_workers_do_not_have_gmail_yet():
    """cost/skill_fit/workload/meeting 은 의도적으로 아직 안 붙였다 — 나중에
    누가 실수로/의도치 않게 다른 워커 파일을 고쳐서 붙이는 건 상관없지만,
    최소한 지금 시점의 의도(priority·risk 한정)를 회귀로 남겨둔다."""
    from app.workers.hr.skill_fit import SPEC as skill_fit_spec
    from app.workers.hr.workload import SPEC as workload_spec
    from app.workers.meeting.adapter import SPEC as meeting_spec
    from app.workers.project.cost import SPEC as cost_spec

    for spec in (cost_spec, skill_fit_spec, workload_spec, meeting_spec):
        assert spec.async_tools is None, f"{spec.node_name} 에 예상 밖의 async_tools"


# ─── run_worker() 가 async_tools 를 실제로 tools 와 합치는지 ────────

class _DummyResult(BaseModel):
    note: str = "ok"


async def test_run_worker_merges_async_tools_into_tool_loop(monkeypatch):
    """WorkerSpec.tools(정적) + async_tools(동적)가 둘 다 run_tool_loop 로
    넘어가는지 — 하나만 넘어가면 gmail 도구가 실제로는 안 쓰이는데 쓰이는
    것처럼 보일 수 있다."""
    import app.workers.base as workers_base

    static_tool = _fake_tool("static_read_tool")
    dynamic_tool = _fake_tool("gmail_search_emails")

    async def fake_async_tools():
        return [dynamic_tool]

    spec = WorkerSpec(
        domain="project",
        dimension="test_dimension",
        role="테스트 역할",
        method="테스트 절차",
        result_model=_DummyResult,
        tools=(static_tool,),
        async_tools=fake_async_tools,
        context_sections=(),
    )

    captured: dict = {}

    async def fake_run_tool_loop(messages, tools, **kwargs):
        captured["tool_names"] = [t.name for t in tools]
        return llm_client.ToolLoopResult(messages=list(messages))

    class _Envelope:
        result = _DummyResult()
        reasoning = "테스트 근거"
        confidence = 0.7
        evidence: list = []

    async def fake_structured_call(messages, schema, **kwargs):
        return _Envelope()

    monkeypatch.setattr(workers_base.llm_client, "run_tool_loop", fake_run_tool_loop)
    monkeypatch.setattr(workers_base.llm_client, "structured_call", fake_structured_call)

    payload = {
        "plan": AnalysisPlan(objective="테스트"),
        "context": AnalysisContext(as_of=date(2026, 8, 9)),
        "attempt": 1,
        "feedback": [],
    }

    output = await run_worker(spec, payload)

    assert set(captured["tool_names"]) == {"static_read_tool", "gmail_search_emails"}
    assert output.error is None


async def test_run_worker_survives_async_tools_failure(monkeypatch):
    """gmail-mcp 가 죽어 있어도(async_tools 가 예외를 던져도) 워커 전체가
    실패하면 안 된다 — 정적 도구만으로라도 계속 진행해야 한다."""
    import app.workers.base as workers_base

    static_tool = _fake_tool("static_read_tool")

    async def failing_async_tools():
        raise RuntimeError("gmail-mcp 다운")

    spec = WorkerSpec(
        domain="project",
        dimension="test_dimension",
        role="테스트 역할",
        method="테스트 절차",
        result_model=_DummyResult,
        tools=(static_tool,),
        async_tools=failing_async_tools,
        context_sections=(),
    )

    captured: dict = {}

    async def fake_run_tool_loop(messages, tools, **kwargs):
        captured["tool_names"] = [t.name for t in tools]
        return llm_client.ToolLoopResult(messages=list(messages))

    class _Envelope:
        result = _DummyResult()
        reasoning = "테스트 근거"
        confidence = 0.7
        evidence: list = []

    async def fake_structured_call(messages, schema, **kwargs):
        return _Envelope()

    monkeypatch.setattr(workers_base.llm_client, "run_tool_loop", fake_run_tool_loop)
    monkeypatch.setattr(workers_base.llm_client, "structured_call", fake_structured_call)

    payload = {
        "plan": AnalysisPlan(objective="테스트"),
        "context": AnalysisContext(as_of=date(2026, 8, 9)),
        "attempt": 1,
        "feedback": [],
    }

    output = await run_worker(spec, payload)

    assert captured["tool_names"] == ["static_read_tool"]  # gmail 도구는 안 섞임
    assert output.error is None  # 워커 자체는 실패하지 않음
