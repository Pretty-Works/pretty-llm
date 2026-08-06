"""
Engine B Runner — 에이전트 파이프라인 진입 함수 (계약 개조: 2026-08-06 합의)

담당자 1이 담당자 3의 양해를 받아 이 함수를 계약형으로 개조했다.
호출자는 두 곳: ① 오케스트레이터 engine_b 직행 (api/agent.py)
             ② 엔진 A 의 analyze_impact 도구 (실행 중 심층 분석)

계약 (이 형식만 지키면 내부는 자유):
    async for ev in run_engine_b(goal, run_id):
        ev == {"type": "progress", "text": "사용자에게 보일 한국어 한 줄"}   # 여러 번
        ev == {"type": "result", "answer": "분석 요약", "detail": {...}}      # 마지막 1회

★ 담당자 3에게 — 내부 교체 지점은 _analyze() 하나다. 멀티에이전트 그래프가
  준비되면 _analyze() 본문만 갈아끼우면 된다 (progress 는 yield 대신
  emit 콜백으로 흘리면 됨). 현재 graph.py 는 import 가 깨져 있어
  (run_analysis_router · run_synthesis · run_validator 미존재) 물리지 못했고,
  그동안 아래의 기본 분석(내부 API 조회 + LLM 종합)이 자리를 지킨다.

★ 데이터 조회는 반드시 clients/backend.py 를 쓴다 — 공개 API 직접 호출은
  FastAPI 에 사용자 토큰이 없어 실서버에서 401 이 난다. backend.get() 은
  X-Run-Id 인증 자동 + mock 스위치를 공유한다.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from langchain.chat_models import init_chat_model

from app.clients.backend import backend
from app.config import settings

_SYNTH_PROMPT = """당신은 프로젝트 리스크 분석가입니다. 아래 실데이터를 근거로
질문에 답하세요. 데이터에 없는 것은 지어내지 말고, 판단 근거를 함께 적으세요.

형식: ① 핵심 결론 1~2문장 ② 근거 (데이터 인용) ③ 권고 1~2개. 전체 8문장 이내."""

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        _llm = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
    return _llm


async def run_engine_b(goal: str, run_id: str,
                       screen: str = "HOME") -> AsyncIterator[dict[str, Any]]:
    """심층 분석 실행. progress 여러 번 → result 1회."""
    yield {"type": "progress", "text": "분석할 데이터를 모으고 있어요"}

    # ── 근거 데이터 수집 (X-Run-Id 스코프 조회) ───────────────
    facts: dict[str, Any] = {}
    projects = (await backend.get("/projects", run_id=run_id)).get("projects", [])
    facts["projects"] = projects

    if projects:
        pid = projects[0]["projectId"]        # 질문에 프로젝트 특정이 없으면 첫 참여 프로젝트
        yield {"type": "progress", "text": f"'{projects[0]['name']}' 의 일정·할일을 살펴보고 있어요"}
        facts["milestones"] = await backend.get(f"/projects/{pid}/milestones", run_id=run_id)
        facts["tasks"] = await backend.get("/tasks", run_id=run_id, projectId=pid)
        yield {"type": "progress", "text": "예산과 일정 부담을 확인하고 있어요"}
        facts["budget"] = await backend.get(f"/projects/{pid}/budget", run_id=run_id)
        facts["schedules"] = await backend.get(
            "/schedules", run_id=run_id,
            **{"from": projects[0]["startDate"], "to": projects[0]["targetDate"]})

    # ── 종합 (담당자 3 그래프의 교체 지점) ────────────────────
    yield {"type": "progress", "text": "수집한 근거를 종합해 분석하고 있어요"}
    answer = (await _analyze(goal, facts)).strip()

    yield {"type": "result", "answer": answer,
           "detail": {"sources": {k: True for k in facts}, "engine": "baseline"}}


async def _analyze(goal: str, facts: dict) -> str:
    """★ 교체 지점 — 지금은 LLM 1콜 종합, 이후 담당자 3의 멀티에이전트 그래프."""
    import json
    r = await _get_llm().ainvoke([
        {"role": "system", "content": _SYNTH_PROMPT},
        {"role": "user", "content": f"질문: {goal}\n\n실데이터:\n"
                                    f"{json.dumps(facts, ensure_ascii=False, default=str)[:6000]}"},
    ])
    return r.content if isinstance(r.content, str) else str(r.content)
