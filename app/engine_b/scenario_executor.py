"""
Scenario Executor — Replanning 조정안 3개 병렬 분석 (담당자3)

팀 Engine B 파이프라인을 그대로 재사용한다:
    route → build_context (1회) → analyze_scenario ×3 (workers → validator → synthesis)

- 조정안(ScenarioSpec)은 overrides(dict)로만 표현한다. context_builder.render_context 가
  워커 프롬프트에 "## 적용할 조정안 … 이 조정이 적용된 상태를 전제로 분석하라" 로 주입하므로,
  context 는 1회만 수집해 3개 조정안이 공유하고, 조정안별로 overrides 만 달라지면 분석이 갈라진다.
- analyze_scenario 가 조정안 1개당 SynthesisResult 1개를 돌려준다.
- 결과 3개를 Tradeoff Agent 로 넘겨 비교한다.

★ 시나리오 '유형'은 코드로 고정(결정론적 골격), 각 안의 분석 '내용'은 워커가 채운다.
★ 이 파일은 DB 를 건드리지 않는다. SynthesisResult.proposed_changes 는 '제안'일 뿐,
  실제 반영은 HITL 승인 이후 백엔드가 수행한다.
"""
from __future__ import annotations

import asyncio

from app.engine_b.analysis_router import route
from app.engine_b.context_builder import build_context
from app.engine_b.graph import analyze_scenario
from app.schemas.state import (
    AnalysisContext,
    AnalysisPlan,
    AnalysisRequest,
    Mode,
    ScenarioSpec,
    SynthesisResult,
)


# ── 재계획 유형 카탈로그 (scenario_id → 한글 라벨, 단일 출처) ──
#     tradeoff / 이벤트 변환도 여기서 라벨을 가져다 쓴다.
SCENARIO_LABELS = {
    "extend": "일정연장",
    "add_resource": "인력추가",
    "reduce_scope": "범위축소",
}


# ── 고정 조정안 3종 (결정론적 골격) ───────────────────────────

def build_scenario_specs(plan: AnalysisPlan) -> list[ScenarioSpec]:
    """재계획 유형 3개를 ScenarioSpec 으로 만든다.

    조정폭(인원/예산)은 Router 가 뽑아둔 plan.entities 에 값이 있으면 반영하고,
    없으면 기본값을 쓴다. (scenario_id 는 'base' 가 아니어야 프롬프트에 주입된다)
    """
    ent = plan.entities
    headcount = (
        f"+{ent.headcount_delta}명"
        if ent.headcount_delta and ent.headcount_delta > 0
        else "+1명"
    )

    add_overrides: dict[str, str] = {"인원": headcount}
    if ent.budget_delta and ent.budget_delta > 0:
        add_overrides["예산"] = f"+{ent.budget_delta:,}원"

    return [
        ScenarioSpec(
            scenario_id="extend",
            label=SCENARIO_LABELS["extend"],
            description="인력·범위는 유지하고 마감일을 뒤로 민다.",
            overrides={"마감": "+2주"},
        ),
        ScenarioSpec(
            scenario_id="add_resource",
            label=SCENARIO_LABELS["add_resource"],
            description="마감·범위는 유지하고 인력을 추가 투입한다.",
            overrides=add_overrides,
        ),
        ScenarioSpec(
            scenario_id="reduce_scope",
            label=SCENARIO_LABELS["reduce_scope"],
            description="마감·인력은 유지하고 비핵심 태스크를 제외/연기한다.",
            overrides={"범위": "비핵심 태스크 제외/연기"},
        ),
    ]


# ── 조정안 1개 분석 ───────────────────────────────────────────

async def _analyze_one(
    plan: AnalysisPlan,
    context: AnalysisContext,
    spec: ScenarioSpec,
) -> SynthesisResult:
    result = await analyze_scenario(plan, context, spec)
    # synthesis 가 scenario_id 를 안 채웠을 경우를 대비해 식별자를 보장한다
    # (Tradeoff 가 결과를 조정안에 다시 매핑할 수 있어야 하므로)
    if not result.scenario_id or result.scenario_id == "base":
        result.scenario_id = spec.scenario_id
    return result


# ── 메인 실행 ─────────────────────────────────────────────────

async def run(
    request: AnalysisRequest,
    plan: AnalysisPlan | None = None,
) -> list[SynthesisResult]:
    """조정안 3개를 병렬 분석해 SynthesisResult 3개를 반환한다.

    plan 을 안 주면 replan 모드로 라우팅한다(호출부가 이미 route 했으면 그걸 넘긴다).
    context 는 1회만 만들어 3개 조정안이 공유한다.
    """
    plan = plan or await route(request, force_mode=Mode.replan)
    context: AnalysisContext = await build_context(plan, request)

    specs = build_scenario_specs(plan)
    results: list[SynthesisResult] = await asyncio.gather(
        *(_analyze_one(plan, context, spec) for spec in specs)
    )
    return list(results)
