"""
Replan 파이프라인 — 조정안 생성 → 비교·추천 → 화면용 이벤트까지 (담당자3)

    route(replan) → scenario_executor (조정안 3개 병렬 분석) → tradeoff (비교·추천)
                  → to_question_event (3안 비교를 사용자 선택 question 이벤트로)

엔진 B 진입부는 plan.mode == Mode.replan 이면 run_replan 을 부르고,
결과의 tradeoff 를 to_question_event 로 바꿔 SSE(question)로 흘리면 된다.

★ 여기까지는 '제안 생성'이다. recommended 는 추천일 뿐, 실제 DB 반영은
  사용자 승인(HITL) 이후 백엔드가 scenarios[선택].proposed_changes 로 수행한다.
  이 파일은 DB 를 건드리지 않는다.
"""
from __future__ import annotations

from pydantic import BaseModel

from app.engine_b.analysis_router import route
from app.engine_b.scenario_executor import SCENARIO_LABELS, run as _run_scenarios
from app.engine_b.tradeoff import run as _run_tradeoff
from app.schemas.response import QuestionEvent, QuestionOption
from app.schemas.state import (
    AnalysisPlan,
    AnalysisRequest,
    Mode,
    SynthesisResult,
    TradeoffResult,
)


class ReplanResult(BaseModel):
    """replan 결과 묶음.

    - tradeoff  : 화면에 보여줄 3안 비교 + 추천 (to_question_event 로 변환)
    - scenarios : 승인 시 실제 반영에 쓸 조정안별 종합 결과
                  (선택된 안의 proposed_changes 가 WRITE 액션이 된다)
    """

    tradeoff: TradeoffResult
    scenarios: list[SynthesisResult]


async def run_replan(
    request: AnalysisRequest,
    plan: AnalysisPlan | None = None,
) -> ReplanResult:
    """replan 요청 1건을 '조정안 3개 분석 → 비교·추천'까지 돌린다.

    plan 을 안 주면 replan 모드로 라우팅한다(진입부가 이미 route 했으면 그걸 넘긴다).
    """
    plan = plan or await route(request, force_mode=Mode.replan)
    scenarios = await _run_scenarios(request, plan)
    decision = await _run_tradeoff(scenarios)
    return ReplanResult(tradeoff=decision, scenarios=scenarios)


def to_question_event(tradeoff: TradeoffResult) -> QuestionEvent:
    """replan 비교·추천을 사용자 선택용 question 이벤트로 변환한다.

    옵션 3개(조정안) + 추천 표시. 사용자가 하나 고르면 그 scenario_id 가 결정으로 돌아온다.
    (SSE 로 내보낼 땐: common.sse.sse_event("question", ev.model_dump(mode="json")))
    """
    rec = tradeoff.recommended_scenario

    options: list[QuestionOption] = []
    for c in tradeoff.comparisons:
        sid = c.scenario_type  # scenario_id 가 담겨 있음
        label = SCENARIO_LABELS.get(sid, sid)
        axes = (
            f"일정회복 {c.schedule_recovery} · "
            f"비용 {c.cost_impact} · 리스크 {c.risk_level}"
        )
        desc = f"{axes} — {c.summary}" if c.summary else axes
        if sid == rec:
            label = f"{label} (추천)"
            if tradeoff.tradeoffs:
                desc += " / 감수: " + ", ".join(tradeoff.tradeoffs)
        options.append(QuestionOption(id=sid, label=label, description=desc))

    rec_label = SCENARIO_LABELS.get(rec, rec)
    text = f"재계획 방안 3가지 중 하나를 선택해 주세요. 추천은 '{rec_label}'예요"
    if tradeoff.reason:
        text += f" — {tradeoff.reason}"
    if len(text) > 200:
        text = text[:197] + "..."

    return QuestionEvent(text=text, options=options, multiple=False, allowFreeText=True)
