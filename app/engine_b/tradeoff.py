"""
Tradeoff Agent — Replanning 최종 비교·추천 (담당자3)

scenario_executor 가 만든 SynthesisResult 3개(조정안별 종합 분석)를 받아
'일정 회복 · 비용 · 리스크' 3축으로 비교하고, 현재 프로젝트에 가장 적절한
조정안 1개를 추천한다.

★ recommended 는 '추천'이지 '확정'이 아니다.
  결과(TradeoffResult)는 HITL 제안으로 나가 사용자가 approve/reject/replan 하고,
  실제 DB 반영은 승인 이후 백엔드가 수행한다. 이 파일은 DB 를 건드리지 않는다.

입력: list[SynthesisResult]   (scenario_id 로 서로 구분됨)
출력: TradeoffResult
"""
from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.config import settings
from app.prompts.tradeoff import TRADEOFF_SYSTEM, TRADEOFF_USER
from app.schemas.state import ScenarioComparison, SynthesisResult, TradeoffResult
from app.utils.parser import parse_json_response

llm = ChatOpenAI(model=settings.llm_model, api_key=settings.llm_api_key)


# ── 포맷 헬퍼 ─────────────────────────────────────────────────

def _format_results(results: list[SynthesisResult]) -> str:
    """SynthesisResult 3개를 비교용 텍스트로. 각 블록 머리에 [scenario_id]."""
    blocks = []
    for r in results:
        actions = "; ".join(a.what for a in r.actions[:5]) if r.actions else "없음"
        risks = ", ".join(r.residual_risks) if r.residual_risks else "없음"
        blocks.append(
            f"[{r.scenario_id}] {r.headline or '(제목 없음)'}\n"
            f"- 요약: {r.summary or '없음'}\n"
            f"- 핵심 조치: {actions}\n"
            f"- 잔여 리스크: {risks}\n"
            f"- confidence: {r.confidence:.2f}"
        )
    return "\n\n".join(blocks)


# ── 검증 ──────────────────────────────────────────────────────

def _validate(raw: dict, valid_ids: set[str]) -> bool:
    if raw.get("recommended_scenario") not in valid_ids:
        return False
    if not raw.get("reason"):
        return False
    if not raw.get("comparisons"):
        return False
    return True


# ── 메인 실행 ─────────────────────────────────────────────────

async def run(results: list[SynthesisResult]) -> TradeoffResult:
    valid_ids = {r.scenario_id for r in results}

    user_msg = TRADEOFF_USER.format(scenarios=_format_results(results))

    # 최대 2회 재시도
    raw: dict = {}
    for attempt in range(2):
        try:
            response = await llm.ainvoke([
                {"role": "system", "content": TRADEOFF_SYSTEM},
                {"role": "user", "content": user_msg},
            ])
            raw = parse_json_response(response.content)
            if _validate(raw, valid_ids):
                break
        except Exception:
            if attempt == 1:
                raise

    comparisons = [
        ScenarioComparison(
            scenario_type=c.get("scenario_id") or c.get("scenario_type", ""),
            schedule_recovery=c.get("schedule_recovery", ""),
            cost_impact=c.get("cost_impact", ""),
            risk_level=c.get("risk_level", ""),
            summary=c.get("summary", ""),
        )
        for c in raw.get("comparisons", [])
    ]

    # 추천이 유효 시나리오가 아니면 첫 시나리오로 폴백 (안전장치)
    recommended = raw.get("recommended_scenario")
    if recommended not in valid_ids:
        recommended = results[0].scenario_id if results else ""

    return TradeoffResult(
        recommended_scenario=recommended,
        reason=raw.get("reason", ""),
        comparisons=comparisons,
        tradeoffs=raw.get("tradeoffs", []),
        confidence=float(raw.get("confidence", 0.7)),
    )
