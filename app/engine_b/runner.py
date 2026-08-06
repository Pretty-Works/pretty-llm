# app/engine_b/runner.py
"""Engine B 실행기 — 에이전트 파이프라인 계약 + 멀티에이전트 그래프 (병합: 2026-08-06)

계약 (호출자: ① api/agent.py 직행 ② tools/analyze.py 의 analyze_impact):
    async for ev in run_engine_b(goal, run_id):
        ev == {"type": "progress", "text": "사용자에게 보일 한국어 한 줄"}   # 여러 번
        ev == {"type": "result", "answer": "분석 요약", "detail": {...}}      # 마지막 1회

내부 구성 (충돌 병합의 결과):
    1차 — 담당자 3의 멀티에이전트 그래프 (analysis_router → context_builder
          → 워커 병렬 → validator → synthesis). shuu6·7 로 조립 완료된 실물.
    2차 — 그래프가 실패하면 기본 분석(내부 API 조회 + LLM 종합)으로 폴백해
          스트림이 죽지 않게 한다.

신원: 그래프 입력(AnalysisRequest)이 user_id 를 요구하므로 user.me(X-Run-Id
역산)로 조회해 채운다. FastAPI 는 userId 를 직접 받지 않는다 (규격).
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, AsyncIterator

from langchain.chat_models import init_chat_model

from app.clients.backend import backend
from app.config import settings

# 노드 이름 → 사용자에게 보일 진행 문구. 워커 노드는 이름이 동적이라 기본 문구로.
_NODE_TEXT = {
    "analysis_router": "분석 경로를 정하고 있어요",
    "context_builder": "관련 데이터를 모으고 있어요",
    "validator": "분석 결과를 검증하고 있어요",
    "synthesis": "결과를 종합하고 있어요",
}
_WORKER_TEXT = "여러 관점에서 나눠 분석하고 있어요"


@lru_cache(maxsize=1)
def _graph():
    from app.engine_b.graph import build_engine_b_graph
    return build_engine_b_graph()


async def run_engine_b(goal: str, run_id: str,
                       screen: str = "HOME") -> AsyncIterator[dict[str, Any]]:
    """심층 분석 실행. progress 여러 번 → result 1회. 그래프 실패 시 기본 분석 폴백."""
    try:
        async for ev in _run_graph(goal, run_id):
            yield ev
        return
    except Exception as exc:                     # noqa: BLE001 — 데모 연속성 우선
        yield {"type": "progress",
               "text": f"정밀 분석이 잠시 어려워 기본 분석으로 전환합니다"}
        # (원인은 서버 로그로) — print 는 uvicorn 로그에 남는다
        print(f"[engine_b] 그래프 실패, 기본 분석 폴백: {type(exc).__name__}: {exc}")

    async for ev in _run_baseline(goal, run_id):
        yield ev


# ─────────────────────────────────────────────────────────────
# 1차 — 담당자 3의 멀티에이전트 그래프
# ─────────────────────────────────────────────────────────────
async def _run_graph(goal: str, run_id: str) -> AsyncIterator[dict[str, Any]]:
    from app.schemas.state import AnalysisRequest

    # 그래프는 user_id 를 요구한다 — X-Run-Id 역산(user.me)으로 채운다
    me = await backend.get("/me", run_id=run_id)
    request = AnalysisRequest(query=goal, user_id=me["userId"])
    initial = {"request": request, "worker_outputs": [], "trace": []}

    result_model = None
    async for event in _graph().astream(initial):
        node, update = next(iter(event.items()))
        yield {"type": "progress",
               "text": _NODE_TEXT.get(node, _WORKER_TEXT)}
        if isinstance(update, dict) and update.get("result") is not None:
            result_model = update["result"]

    if result_model is None:
        raise RuntimeError("그래프가 result 없이 종료됨")

    yield {"type": "result",
           "answer": _render(result_model),
           "detail": result_model.model_dump(mode="json")}


def _render(r) -> str:
    """SynthesisResult → 사용자에게 보일 답변 문장."""
    parts = [p for p in (r.headline, r.summary) if p]
    if r.residual_risks:
        parts.append("잔여 위험: " + " / ".join(r.residual_risks[:3]))
    if r.proposed_changes:
        parts.append(f"제안된 변경 {len(r.proposed_changes)}건이 있습니다 — "
                     "적용을 원하시면 말씀해 주세요.")
    return "\n".join(parts) or "분석을 마쳤지만 요약을 만들지 못했습니다."


# ─────────────────────────────────────────────────────────────
# 2차 — 기본 분석 폴백 (내부 API 조회 + LLM 종합)
# ─────────────────────────────────────────────────────────────
_SYNTH_PROMPT = """당신은 프로젝트 리스크 분석가입니다. 아래 실데이터를 근거로
질문에 답하세요. 데이터에 없는 것은 지어내지 말고, 판단 근거를 함께 적으세요.

형식: ① 핵심 결론 1~2문장 ② 근거 (데이터 인용) ③ 권고 1~2개. 전체 8문장 이내."""

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        _llm = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
    return _llm


async def _run_baseline(goal: str, run_id: str) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "progress", "text": "분석할 데이터를 모으고 있어요"}

    facts: dict[str, Any] = {}
    projects = (await backend.get("/projects", run_id=run_id)).get("projects", [])
    facts["projects"] = projects

    if projects:
        pid = projects[0]["projectId"]
        yield {"type": "progress", "text": f"'{projects[0]['name']}' 의 일정·할일을 살펴보고 있어요"}
        facts["milestones"] = await backend.get(f"/projects/{pid}/milestones", run_id=run_id)
        facts["tasks"] = await backend.get("/tasks", run_id=run_id, projectId=pid)
        facts["budget"] = await backend.get(f"/projects/{pid}/budget", run_id=run_id)

    yield {"type": "progress", "text": "수집한 근거를 종합해 분석하고 있어요"}
    r = await _get_llm().ainvoke([
        {"role": "system", "content": _SYNTH_PROMPT},
        {"role": "user", "content": f"질문: {goal}\n\n실데이터:\n"
                                    f"{json.dumps(facts, ensure_ascii=False, default=str)[:6000]}"},
    ])
    answer = (r.content if isinstance(r.content, str) else str(r.content)).strip()

    yield {"type": "result", "answer": answer,
           "detail": {"sources": {k: True for k in facts}, "engine": "baseline"}}
