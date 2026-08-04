# app/engine_b/runner.py
"""Engine B 실행기 — 그래프를 돌리며 노드가 끝날 때마다 SSE 한 줄을 흘린다.

    async for chunk in run_engine_b(request):
        yield chunk

이벤트 형태:
    data: {"step": "analysis_router", "status": "done"}
    data: {"step": "synthesis", "status": "done", "result": {...}}
    data: {"step": "done"}
"""

import json
from functools import lru_cache
from typing import AsyncIterator

from app.engine_b.graph import build_engine_b_graph
from app.schemas.state import AnalysisRequest


@lru_cache(maxsize=1)
def _graph():
    return build_engine_b_graph()


async def run_engine_b(request: AnalysisRequest) -> AsyncIterator[str]:
    """질의 1건을 끝까지 돌린다."""
    initial = {"request": request, "worker_outputs": [], "trace": []}

    async for event in _graph().astream(initial):
        node, update = next(iter(event.items()))
        if node == "synthesis":
            result = update["result"].model_dump(mode="json")
            yield _sse({"step": node, "status": "done", "result": result})
        else:
            yield _sse({"step": node, "status": "done"})

    yield _sse({"step": "done"})


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
