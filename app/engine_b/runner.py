"""
Engine B Runner — LangGraph 그래프 실행 + SSE 스트리밍

외부(api/)에서 이렇게 호출:
    async for chunk in run_engine_b(state):
        yield chunk   # SSE data

담당자3 작성.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from app.engine_b.graph import build_engine_b_graph
from app.schemas.state import EngineBState

_graph = build_engine_b_graph()


async def run_engine_b(state: EngineBState) -> AsyncIterator[str]:
    """
    Engine B 실행. 각 노드 완료 시 SSE 이벤트 yield.

        data: {"step": "analysis_router", "status": "done"}
        data: {"step": "meeting_workers", "status": "done"}
        data: {"step": "validator", "status": "done"}
        data: {"step": "synthesis", "status": "done", "result": {...}}
        data: {"step": "done"}
    """
    async for event in _graph.astream(state):
        node_name = next(iter(event))
        updated: EngineBState = event[node_name]

        if node_name == "synthesis":
            yield _sse({"step": node_name, "status": "done", "result": updated.synthesis})
        else:
            yield _sse({"step": node_name, "status": "done"})

    yield _sse({"step": "done"})


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
