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
#     ★ 2026-08-09 BE 스펙 개정 — scenarioType 값이 대문자 3종(REALLOCATE/EXTEND/
#       REDUCE_SCOPE)으로 확정됐다. 예전엔 extend/add_resource/reduce_scope 소문자였다.
SCENARIO_LABELS = {
    "EXTEND": "일정 연장",
    "REALLOCATE": "인력 재배치",
    "REDUCE_SCOPE": "범위 축소",
}


# ── 고정 조정안 3종 (결정론적 골격) ───────────────────────────

def build_scenario_specs(plan: AnalysisPlan) -> list[ScenarioSpec]:
    """재계획 유형 3개를 ScenarioSpec 으로 만든다.

    조정폭(인원)은 Router 가 뽑아둔 plan.entities 에 값이 있으면 반영하고,
    없으면 기본값을 쓴다. (scenario_id 는 'base' 가 아니어야 프롬프트에 주입된다)

    ★ 예산(budget) 조정 override 는 넣지 않는다 — BE 에 프로젝트 예산을 바꾸는
      operation 자체가 없어서(REPLAN API 스펙 참고), 워커에게 "예산이 늘었다"고
      전제를 줘봤자 반영 못 할 제안만 만들게 된다.
    """
    ent = plan.entities
    headcount = (
        f"+{ent.headcount_delta}명"
        if ent.headcount_delta and ent.headcount_delta > 0
        else "+1명"
    )

    return [
        ScenarioSpec(
            scenario_id="EXTEND",
            label=SCENARIO_LABELS["EXTEND"],
            description="인력·범위는 유지하고 마감일을 뒤로 민다.",
            overrides={"마감": "+2주"},
        ),
        ScenarioSpec(
            scenario_id="REALLOCATE",
            label=SCENARIO_LABELS["REALLOCATE"],
            description="마감·범위는 유지하고 인력을 추가 투입한다.",
            overrides={"인원": headcount},
        ),
        ScenarioSpec(
            scenario_id="REDUCE_SCOPE",
            label=SCENARIO_LABELS["REDUCE_SCOPE"],
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

# 조정안 3개를 전부 동시에 돌리면 워커 5개 × 3안 = 15개 체인이 한꺼번에 LLM을 때려서
# 분당 토큰(TPM) 한도에 쉽게 걸린다(gpt-4o-mini 기준 실측으로 확인됨). 2개로 제한해
# 순간 부하를 낮춘다 — 전체 지연은 조금 늘지만 완주 확률이 오른다.
_SCENARIO_CONCURRENCY = 2


async def run(
    request: AnalysisRequest,
    plan: AnalysisPlan | None = None,
) -> list[SynthesisResult]:
    """조정안 3개를 분석해 SynthesisResult 3개를 반환한다(동시 최대 _SCENARIO_CONCURRENCY개).

    plan 을 안 주면 replan 모드로 라우팅한다(호출부가 이미 route 했으면 그걸 넘긴다).
    context 는 1회만 만들어 3개 조정안이 공유한다.
    """
    plan = plan or await route(request, force_mode=Mode.replan)
    context: AnalysisContext = await build_context(plan, request)

    specs = build_scenario_specs(plan)
    sem = asyncio.Semaphore(_SCENARIO_CONCURRENCY)

    async def _bounded(spec: ScenarioSpec) -> SynthesisResult:
        async with sem:
            return await _analyze_one(plan, context, spec)

    results: list[SynthesisResult] = await asyncio.gather(
        *(_bounded(spec) for spec in specs)
    )
    return list(results)
