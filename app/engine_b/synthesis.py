# app/engine_b/synthesis.py
"""Synthesis Agent — 한 안(案) 내부의 상충 통합.

워커는 각자 자기 축만 본다. 그래서 결론이 서로 부딪히는 게 정상이고,
그걸 나란히 나열하는 건 통합이 아니다. 부딪히는 지점을 찾아 결론 하나로 만드는 게 이 단계다.

여러 조정안 **사이**의 비교는 여기가 아니라 Tradeoff Agent(담당자 3)의 몫이다.
"""

import json

from langchain_core.messages import HumanMessage, SystemMessage

from app.common import llm_client
from app.prompts import synthesis as prompt
from app.schemas.state import (
    AnalysisContext,
    AnalysisPlan,
    Evidence,
    ScenarioSpec,
    SynthesisResult,
    ValidationReport,
    WorkerOutput,
)
from app.utils.logger import get_logger
from app.utils.parser import truncate

log = get_logger("engine_b.synthesis")

# 워커 result 가 길어지면 통합 프롬프트가 통째로 부풀어 오른다.
_RESULT_CHAR_LIMIT = 2500


# ─── 공개 진입점 ──────────────────────────────────────────────────

async def synthesize(
    outputs: list[WorkerOutput],
    plan: AnalysisPlan,
    context: AnalysisContext,
    validation: ValidationReport | None = None,
    scenario: ScenarioSpec | None = None,
    apply_feedback: str | None = None,
) -> SynthesisResult:
    """워커 결과들을 결론 하나로 통합한다.

    apply_feedback: ★ 8/12 추가 — 직전 시도의 proposed_changes 가
      app/engine_b/apply_builder.build_operations() 를 통과 못 했을 때, 그 거부
      사유를 여기 실어 넘기면 프롬프트가 "왜 실패했는지" 보고 고쳐서 다시 낸다
      (graph.py _synthesis_node 가 재시도 루프에서 채운다. 없으면 최초 시도).
    """
    scenario = scenario or ScenarioSpec()
    usable = [output for output in outputs if not output.error]

    if not usable:
        log.warning("사용할 수 있는 워커 결과가 없다 -> 통합 생략")
        return _degraded_result(outputs, scenario)

    messages = [
        SystemMessage(content=prompt.SYSTEM),
        HumanMessage(
            content=prompt.USER_TEMPLATE.format(
                objective=plan.objective or "(목표 미상)",
                constraints="\n".join(f"- {c}" for c in plan.constraints) or "- (없음)",
                scenario=_render_scenario(scenario),
                validation=_render_validation(validation),
                worker_results=_render_workers(usable),
                apply_feedback=apply_feedback or "- (없음 — 최초 시도)",
            )
        ),
    ]

    try:
        result = await llm_client.structured_call(
            messages,
            SynthesisResult,
            profile="reasoning",
            component="engine_b.synthesis",
            # proposed_changes 의 before/after 가 자유형 dict 라 엄격 모드가 스키마를 거부한다.
            # 그대로 두면 통합이 매번 실패해 _degraded_result 로 떨어진다.
            method="function_calling",
        )
    except Exception as exc:
        log.error("통합 실패: %s", exc)
        result = _degraded_result(outputs, scenario)
        result.summary = f"통합 단계에서 오류가 발생했다: {exc}"
        return result

    result.scenario_id = scenario.scenario_id
    result.confidence = min(1.0, max(0.0, result.confidence))
     # ★ 회의 Tradeoff 결과를 최종 결과에 전달
    for output in usable:
        if (
            output.domain == "meeting"
            and output.dimension == "tradeoff"
        ):
            result.meeting_ranking = (
                output.result.get("ranked", [])
            )
            break
        
    # 근거는 워커 것을 모아 올린다. 통합 단계가 새 근거를 만들 일은 없다.
    result.evidence = _rollup_evidence(usable)
    if validation:
        result.unresolved_violations = list(validation.errors)

    log.info(
        "통합 완료: 액션 %d개, 상충 %d건, conf=%.2f, DB변경제안 %d건",
        len(result.actions),
        len(result.conflicts),
        result.confidence,
        len(result.proposed_changes),
    )
    return result


# ─── 렌더링 ───────────────────────────────────────────────────────

def _render_workers(outputs: list[WorkerOutput]) -> str:
    blocks = []

    for output in sorted(outputs, key=lambda o: o.dimension):
        payload = json.dumps(
            output.result,
            ensure_ascii=False,
            default=str,
        )

        # 회의 Tradeoff 결과는 최종 순위가 핵심이므로
        # 별도 안내를 붙인다.
        if (
            output.domain == "meeting"
            and output.dimension == "tradeoff"
        ):
            blocks.append(
                "### [meeting/tradeoff] 최종 회의 시간 순위 추천\n"
                "아래 ranked의 rank 1~3을 최종 답변에 반드시 반영하라.\n"
                f"결과: {truncate(payload, _RESULT_CHAR_LIMIT)}\n"
                f"판단 근거: {output.reasoning}"
            )
            continue

        blocks.append(
            f"### [{output.domain}/{output.dimension}] "
            f"confidence={output.confidence:.2f} "
            f"(도구 {output.tool_calls}회, "
            f"시도 {output.attempt}회차)\n"
            f"판단 근거: {output.reasoning}\n"
            f"결과: {truncate(payload, _RESULT_CHAR_LIMIT)}"
        )

    return "\n\n".join(blocks)


def _render_validation(validation: ValidationReport | None) -> str:
    if validation is None or not validation.violations:
        return "- 규칙 위반 없음"
    lines = []
    for violation in validation.violations:
        lines.append(
            f"- [{violation.severity}] {violation.code} "
            f"({violation.dimension or '-'}) {violation.message}"
        )
    lines.append(
        "위에 error 가 남아 있다면 그 축의 결론은 규칙을 어긴 상태다. "
        "결론에 반영하지 말고, 사람이 확인해야 할 사항으로 정리하라."
    )
    return "\n".join(lines)


def _render_scenario(scenario: ScenarioSpec) -> str:
    if scenario.scenario_id == "base":
        return "- (조정안 없음. 현재 계획 그대로 분석한 결과다)"
    lines = [f"- {scenario.label}: {scenario.description}"]
    lines += [f"  - {k}: {v}" for k, v in (scenario.overrides or {}).items()]
    return "\n".join(lines)


def _rollup_evidence(outputs: list[WorkerOutput]) -> list[Evidence]:
    """축마다 중복되는 같은 근거는 한 번만 남긴다."""
    seen: set[tuple[str, str]] = set()
    rolled: list[Evidence] = []
    for output in outputs:
        for item in output.evidence:
            key = (item.ref, truncate(item.detail, 80))
            if key in seen:
                continue
            seen.add(key)
            rolled.append(item)
    return rolled


def _degraded_result(outputs: list[WorkerOutput], scenario: ScenarioSpec) -> SynthesisResult:
    """워커가 전부 실패했을 때도 사용자에게 뭔가는 돌려준다."""
    errors = [f"{o.dimension}: {o.error}" for o in outputs if o.error]
    return SynthesisResult(
        scenario_id=scenario.scenario_id,
        headline="분석을 완료하지 못했다",
        summary=(
            "모든 분석 워커가 실패해 결론을 낼 수 없었다. "
            "잠시 후 다시 시도하거나 관리자에게 문의해야 한다."
        ),
        open_questions=errors or ["실행된 워커가 없다."],
        confidence=0.0,
    )
