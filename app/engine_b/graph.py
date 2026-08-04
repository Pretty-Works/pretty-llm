# app/engine_b/graph.py
"""Engine B 그래프 조립 (LangGraph).

    Analysis Router → Context Builder → [Worker 병렬] → Validator → Synthesis
                                            ↑                  │
                                            └── 위반 시 해당 축만 재생성 ──┘

설계 원칙 두 가지가 코드에 그대로 박혀 있다.
1. 선택된 도메인의 워커 세트는 **항상 전부** 병렬 실행된다. focus 는 강조점일 뿐이다.
2. 재생성은 위반이 잡힌 축만 다시 돈다. 멀쩡한 축까지 다시 돌리면 비용만 든다.

담당자 3(Scenario Executor)을 위한 진입점:
- `build_analysis_core()` : Router/Context Builder 를 건너뛰고 워커부터 시작하는 서브그래프.
                            조정안마다 plan/context/scenario 를 넣어 병렬로 돌리면 된다.
- `analyze_scenario()`    : 위 서브그래프를 한 번 돌려 SynthesisResult 를 받는 헬퍼.
"""

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.config import get_settings
from app.engine_b.analysis_router import route
from app.engine_b.context_builder import build_context
from app.engine_b.synthesis import synthesize
from app.engine_b.validator import validate, validate_synthesis
from app.schemas.state import (
    AnalysisContext,
    AnalysisPlan,
    AnalysisRequest,
    EngineBState,
    ScenarioSpec,
    SynthesisResult,
    TraceEvent,
)
from app.utils.logger import get_logger
from app.workers import registry
from app.workers.base import make_node

log = get_logger("engine_b.graph")


# ─── 노드 ─────────────────────────────────────────────────────────

async def _router_node(state: EngineBState) -> dict[str, Any]:
    plan = await route(state["request"])
    unsupported = registry.unsupported_domains(plan.domains)
    return {
        "plan": plan,
        "trace": [
            TraceEvent(
                node="analysis_router",
                message=plan.reasoning,
                payload={
                    "mode": plan.mode,
                    "domains": plan.domains,
                    "focus": plan.focus,
                    "confidence": plan.confidence,
                    # 라우터가 골랐지만 아직 워커가 없는 도메인 (담당자 1/3 담당분)
                    "unsupported_domains": unsupported,
                },
            )
        ],
    }


async def _context_node(state: EngineBState) -> dict[str, Any]:
    context = await build_context(state["plan"], state["request"])
    return {
        "context": context,
        "trace": [
            TraceEvent(
                node="context_builder",
                message=f"프로젝트 {len(context.projects)}건 / 후보 {len(context.candidates)}명 확보",
                payload={"missing": context.missing},
            )
        ],
    }


def _validator_node(state: EngineBState) -> dict[str, Any]:
    settings = get_settings()
    report = validate(
        state.get("worker_outputs") or [],
        state["context"],
        state.get("plan"),
        state.get("scenario"),
    )

    retry_count = int(state.get("retry_count") or 0)
    dimensions = report.dimensions_to_retry()

    # 재시도 여부는 여기서 한 번만 판단한다. 라우팅 함수는 상태를 못 바꾸기 때문에
    # 같은 판단을 두 곳에서 하면 어긋나서 무한 루프가 된다.
    if not dimensions or retry_count >= settings.validator_max_retries:
        if dimensions:
            log.warning("재시도 한도 초과. 남은 위반 %d건을 그대로 보고한다.", len(report.errors))
        return {
            "validation": report,
            "retry_dimensions": [],
            "trace": [
                TraceEvent(
                    node="validator",
                    message=f"error {len(report.errors)}건 / 총 {len(report.violations)}건",
                    payload={"retry": False, "codes": [v.code for v in report.violations]},
                )
            ],
        }

    return {
        "validation": report,
        "retry_dimensions": dimensions,
        "retry_count": retry_count + 1,
        "feedback": report.feedback_by_dimension(),
        "trace": [
            TraceEvent(
                node="validator",
                message=f"{', '.join(dimensions)} 축 재생성 (시도 {retry_count + 1})",
                payload={"retry": True, "codes": [v.code for v in report.errors]},
            )
        ],
    }


async def _synthesis_node(state: EngineBState) -> dict[str, Any]:
    context = state["context"]
    result = await synthesize(
        state.get("worker_outputs") or [],
        state["plan"],
        context,
        state.get("validation"),
        state.get("scenario"),
    )

    # DB 변경 제안은 사람이 승인하기 직전이므로 한 번 더 훑는다.
    result.unresolved_violations += validate_synthesis(result, context)

    return {
        "result": result,
        "trace": [
            TraceEvent(
                node="synthesis",
                message=result.headline,
                payload={
                    "confidence": result.confidence,
                    "actions": len(result.actions),
                    "conflicts": len(result.conflicts),
                    "requires_approval": result.requires_approval,
                },
            )
        ],
    }


# ─── 라우팅 (fan-out) ─────────────────────────────────────────────

def _dispatch_workers(state: EngineBState) -> list[Send] | str:
    """선택된 도메인의 워커를 전부 병렬로 띄운다."""
    plan: AnalysisPlan = state["plan"]
    context: AnalysisContext = state["context"]
    scenario = state.get("scenario") or ScenarioSpec()

    specs = registry.specs_for_domains(plan.domains)
    if not specs:
        log.warning("실행할 워커가 없다 (domains=%s). 통합 단계로 넘어간다.", plan.domains)
        return "synthesis"

    log.info("워커 %d개 병렬 실행: %s", len(specs), [s.dimension for s in specs])
    return [
        Send(
            spec.node_name,
            {
                "plan": plan,
                "context": context,
                "scenario": scenario,
                "dimension": spec.dimension,
                "feedback": [],
                "attempt": 1,
            },
        )
        for spec in specs
    ]


def _route_after_validation(state: EngineBState) -> list[Send] | str:
    """위반이 잡힌 축만 피드백과 함께 다시 띄운다."""
    dimensions = state.get("retry_dimensions") or []
    if not dimensions:
        return "synthesis"

    plan: AnalysisPlan = state["plan"]
    context: AnalysisContext = state["context"]
    scenario = state.get("scenario") or ScenarioSpec()
    feedback = state.get("feedback") or {}
    attempt = int(state.get("retry_count") or 1) + 1

    sends: list[Send] = []
    for dimension in dimensions:
        spec = registry.spec_by_dimension(dimension)
        if spec is None:
            continue
        sends.append(
            Send(
                spec.node_name,
                {
                    "plan": plan,
                    "context": context,
                    "scenario": scenario,
                    "dimension": dimension,
                    "feedback": feedback.get(dimension, []),
                    "attempt": attempt,
                },
            )
        )
    return sends or "synthesis"


# ─── 조립 ─────────────────────────────────────────────────────────

def _attach_core(builder: StateGraph) -> list[str]:
    """워커 + Validator + Synthesis 를 붙이고, 워커 노드 이름 목록을 돌려준다."""
    worker_nodes: list[str] = []
    for spec in registry.all_specs():
        builder.add_node(spec.node_name, make_node(spec))
        builder.add_edge(spec.node_name, "validator")
        worker_nodes.append(spec.node_name)

    builder.add_node("validator", _validator_node)
    builder.add_node("synthesis", _synthesis_node)
    builder.add_conditional_edges(
        "validator", _route_after_validation, [*worker_nodes, "synthesis"]
    )
    builder.add_edge("synthesis", END)
    return worker_nodes


def build_engine_b_graph():
    """전체 그래프: Router → Context → 워커 병렬 → Validator → Synthesis."""
    builder = StateGraph(EngineBState)
    builder.add_node("analysis_router", _router_node)
    builder.add_node("context_builder", _context_node)

    worker_nodes = _attach_core(builder)

    builder.add_edge(START, "analysis_router")
    builder.add_edge("analysis_router", "context_builder")
    builder.add_conditional_edges(
        "context_builder", _dispatch_workers, [*worker_nodes, "synthesis"]
    )
    return builder.compile()


def build_analysis_core():
    """워커부터 시작하는 서브그래프 (담당자 3의 Scenario Executor 재사용용).

    입력에 plan / context / scenario 를 직접 넣는다. Router 와 Context Builder 는 돌지 않는다.
    """
    builder = StateGraph(EngineBState)
    worker_nodes = _attach_core(builder)
    builder.add_conditional_edges(
        START, _dispatch_workers, [*worker_nodes, "synthesis"]
    )
    return builder.compile()


# ─── 공개 진입점 ──────────────────────────────────────────────────

async def run_analysis(request: AnalysisRequest) -> EngineBState:
    """질의 1건을 끝까지 돌린다. 상태 전체를 돌려주므로 trace 도 함께 볼 수 있다."""
    graph = build_engine_b_graph()
    return await graph.ainvoke({"request": request, "worker_outputs": [], "trace": []})


async def analyze_scenario(
    plan: AnalysisPlan,
    context: AnalysisContext,
    scenario: ScenarioSpec | None = None,
) -> SynthesisResult:
    """조정안 1개를 분석해 통합 결과만 돌려준다 (담당자 3 진입점).

    Scenario Executor 는 조정안 3개에 대해 이 함수를 병렬로 부르면 되고,
    그 결과 3개를 Tradeoff Agent 에 넘겨 비교하면 된다.
    """
    graph = build_analysis_core()
    state = await graph.ainvoke(
        {
            "plan": plan,
            "context": context,
            "scenario": scenario or ScenarioSpec(),
            "worker_outputs": [],
            "trace": [],
        }
    )
    return state["result"]
