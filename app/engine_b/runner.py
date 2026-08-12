# app/engine_b/runner.py
"""Engine B 실행기 — 에이전트 파이프라인 계약 + 멀티에이전트 그래프 (병합: 2026-08-06)

계약 (호출자: ① api/agent.py 직행 ② tools/analyze.py 의 analyze_impact):
    async for ev in run_engine_b(goal, run_id, history=history):
        ev == {"type": "progress", "text": "사용자에게 보일 한국어 한 줄"}   # 여러 번
        ev == {"type": "result", "answer": "분석 요약", "detail": {...}}      # 마지막 1회

내부 구성 (충돌 병합의 결과):
    1차 — 담당자 3의 멀티에이전트 그래프 (analysis_router → context_builder
          → 워커 병렬 → validator → synthesis). shuu6·7 로 조립 완료된 실물.
    2차 — 그래프가 실패하면 기본 분석(내부 API 조회 + LLM 종합)으로 폴백해
          스트림이 죽지 않게 한다.

신원: 그래프 입력(AnalysisRequest)이 user_id 를 요구하므로 user.me(X-Run-Id
역산)로 조회해 채운다. FastAPI 는 userId 를 직접 받지 않는다 (규격).

★ history — simple_query/engine_a/replan은 전부 hitl.stream_run(agent, goal,
  history, ...)으로 직전 대화(messages)를 넘기는데, 이 "순수 분석"(mode=analysis)
  경로만 여태 goal 텍스트 하나만 받고 history를 아예 안 받았다. "아까 그 분석 이어서
  X도 봐줘"처럼 직전 대화를 참조해야 하는 요청이 여기서는 매번 백지 상태로
  시작됐다는 뜻 — 이번에 고쳤다. AnalysisRequest에 이미 chat_history 필드가 있고
  analysis_router.py의 _render_user()도 이미 그걸 프롬프트에 넣게 짜여 있었다
  (engine_b.analysis_router.run()/to_analysis_request()가 쓰던 것과 동일한 필드) —
  즉 라우터 쪽은 처음부터 준비돼 있었는데 이 파일이 그 필드를 안 채워서 안 쓰이고
  있었을 뿐이다. history는 하위 호환을 위해 기본값 None으로 둔다 — analyze_impact
  (도메인 에이전트가 이미 완결된 한 문장으로 question을 만들어 넘기는 경로)는
  history가 따로 필요 없어 그대로 안 넘겨도 된다.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, AsyncIterator

from langchain.chat_models import init_chat_model

from app.clients.backend import backend
from app.common.run_context import current_run_id
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


async def run_engine_b(goal: str, run_id: str, screen: str = "HOME",
                       history: list[dict] | None = None) -> AsyncIterator[dict[str, Any]]:
    """심층 분석 실행. progress 여러 번 → result 1회. 그래프 실패 시 기본 분석 폴백.

    history: [{"role": "USER"|"AGENT", "content": str}, ...] — api/agent.py의
    RunRequest.messages를 그대로 넘겨받은 것과 같은 모양(다른 라우트들과 동일).
    """
    current_run_id.set(run_id)   # 그래프 안 조회 도구들이 X-Run-Id 로 쓴다
    try:
        async for ev in _run_graph(goal, run_id, history):
            yield ev
        return
    except Exception as exc:                     # noqa: BLE001 — 데모 연속성 우선
        yield {"type": "progress",
               "text": f"정밀 분석이 잠시 어려워 기본 분석으로 전환합니다"}
        # (원인은 서버 로그로) — print 는 uvicorn 로그에 남는다
        print(f"[engine_b] 그래프 실패, 기본 분석 폴백: {type(exc).__name__}: {exc}")

    async for ev in _run_baseline(goal, run_id, history):
        yield ev


# ─────────────────────────────────────────────────────────────
# 1차 — 담당자 3의 멀티에이전트 그래프
# ─────────────────────────────────────────────────────────────
async def _run_graph(goal: str, run_id: str,
                     history: list[dict] | None = None) -> AsyncIterator[dict[str, Any]]:
    from app.schemas.state import AnalysisRequest

    # 그래프는 user_id 를 요구한다 — X-Run-Id 역산(user.me)으로 채운다
    me = await backend.get("/me", run_id=run_id)
    # chat_history는 analysis_router._render_user()가 이미 "[직전 대화]" 블록으로
    # 프롬프트에 넣게 짜여 있었다 — 여기서 채워주기만 하면 된다.
    request = AnalysisRequest(query=goal, user_id=me["userId"], chat_history=history or [])
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

    answer = _render(result_model)
    from app.common.background import fire
    from app.memory.indexer import index_analysis
    fire(index_analysis(run_id, goal,
                        getattr(result_model, "headline", None), answer))
    yield {"type": "result", "answer": answer,
           "detail": result_model.model_dump(mode="json")}


def _render(r) -> str:
    parts = [
        p for p in (r.headline, r.summary)
        if p
    ]

    # ★ 8/12 추가 — synthesis 가 만드는 가장 구체적인 부분(actions: 무엇을·누가·
    #   언제·왜)이 지금까지 여기서 빠져 있었다. headline/summary는 synthesis 프롬프트
    #   규칙상("모든 워커 결과를 요약해서 나열하는 것 금지") 원래 짧고 추상적으로
    #   쓰이는데, actions 없이 그 둘 + residual_risks 만 보여주면 "마감일이 임박한
    #   할 일들이 다수 존재" 처럼 구체적인 할 일 이름 하나 없는 두루뭉술한 답만
    #   남는다 — 실제로 어떤 할 일을 어떻게 하라는지가 다 actions 에 있었는데
    #   렌더링에서 누락됐었다.
    if getattr(r, "actions", None):
        lines = ["할 일:"]
        for a in sorted(r.actions, key=lambda x: x.order)[:6]:
            who = f" ({a.who})" if a.who else ""
            when = f" — {a.when}" if a.when else ""
            why = f" : {a.why}" if a.why else ""
            lines.append(f"{a.order}. {a.what}{who}{when}{why}")
        parts.append("\n".join(lines))

    # ★ 회의 추천 순위
    if getattr(r, "meeting_ranking", None):
        lines = ["추천 회의 시간:"]

        for slot in r.meeting_ranking[:3]:
            rank = slot.get("rank", "?")
            start = slot.get("start", "")
            end = slot.get("end", "")
            available = slot.get("available_count", 0)
            total = slot.get("total_count", 0)
            project_fit = slot.get("project_fit", "")
            reason = slot.get("reason", "")

            lines.append(
                f"{rank}순위: {start} ~ {end}\n"
                f"참석 가능: {available}/{total}명\n"
                f"프로젝트 적합도: {project_fit}\n"
                f"이유: {reason}"
            )

        parts.append("\n".join(lines))

    if r.residual_risks:
        parts.append(
            "잔여 위험: "
            + " / ".join(r.residual_risks[:3])
        )

    if r.proposed_changes:
        parts.append(
            f"제안된 변경 {len(r.proposed_changes)}건이 있습니다 — "
            "적용을 원하시면 말씀해 주세요."
        )

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


def _format_history(history: list[dict] | None) -> str:
    """classify.py의 같은 용도 코드와 동일한 모양 — 최근 4턴만, 프롬프트에 붙일 텍스트로."""
    if not history:
        return ""
    recent = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in history[-4:])
    return f"이전 대화:\n{recent}\n\n"


async def _run_baseline(goal: str, run_id: str,
                        history: list[dict] | None = None) -> AsyncIterator[dict[str, Any]]:
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

    yield _sse({"step": "done"})


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
