"""
Engine B LangGraph 그래프 정의

노드 함수 + 엣지 연결 + 그래프 빌드.
실행은 runner.py에서.

담당자3 작성.
"""

from __future__ import annotations

import asyncio

from langgraph.graph import END, StateGraph

from app.engine_b.analysis_router import run_analysis_router
from app.engine_b.synthesis import run_synthesis
from app.engine_b.validator import run_validator
from app.schemas.state import Domain, EngineBState
from app.workers.meeting.project_fit_agent import run as run_project_fit
from app.workers.meeting.schedule_agent import run as run_schedule_agent


# ── 노드 함수 ──────────────────────────────────────────────────

async def node_analysis_router(state: EngineBState) -> EngineBState:
    decision = await run_analysis_router(state)
    return state.model_copy(update={"decision": decision})


async def node_meeting_workers(state: EngineBState) -> EngineBState:
    """schedule_agent + project_fit_agent 병렬 실행"""
    schedule_out, fit_out = await asyncio.gather(
        run_schedule_agent(state),
        run_project_fit(state),
    )
    outputs = list(state.worker_outputs) + [schedule_out, fit_out]
    return state.model_copy(update={"worker_outputs": outputs})


async def node_validator(state: EngineBState) -> EngineBState:
    violations = await run_validator(state)
    return state.model_copy(update={"violations": violations})


async def node_synthesis(state: EngineBState) -> EngineBState:
    synthesis = await run_synthesis(state)
    return state.model_copy(update={"synthesis": synthesis})


# ── 분기 조건 ──────────────────────────────────────────────────

def route_to_worker(state: EngineBState) -> str:
    domain = state.decision.domain
    if domain == Domain.meeting:
        return "meeting_workers"
    # TODO: 다른 도메인 추가 (담당자1/2와 합의)
    return "meeting_workers"


# ── 그래프 빌드 ────────────────────────────────────────────────

def build_engine_b_graph():
    g = StateGraph(EngineBState)

    g.add_node("analysis_router", node_analysis_router)
    g.add_node("meeting_workers", node_meeting_workers)
    g.add_node("validator", node_validator)
    g.add_node("synthesis", node_synthesis)

    g.set_entry_point("analysis_router")

    g.add_conditional_edges(
        "analysis_router",
        route_to_worker,
        {"meeting_workers": "meeting_workers"},
    )

    g.add_edge("meeting_workers", "validator")
    g.add_edge("validator", "synthesis")
    g.add_edge("synthesis", END)

    return g.compile()
