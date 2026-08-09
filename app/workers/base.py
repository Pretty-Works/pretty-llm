# app/workers/base.py
"""워커 공통 실행기.

Engine B 의 워커는 보는 축만 다르고 실행 방식은 전부 같다.
  시스템 프롬프트 조립 → 컨텍스트 제시 → (근거 부족 시) 도구 자율 호출 → 구조화 출력

그래서 축마다 다른 것(역할·판단절차·결과 스키마·쓸 수 있는 도구)만 `WorkerSpec` 으로 선언하고,
돌리는 코드는 여기 하나만 둔다. 새 축을 추가할 때 이 파일은 건드리지 않아도 된다.
"""

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field, create_model

from app.common import llm_client
from app.config import get_settings
from app.engine_b.context_builder import ALL_SECTIONS, render_context
from app.prompts.rubrics import compose_worker_system, feedback_block
from app.schemas.state import (
    AnalysisContext,
    AnalysisPlan,
    Domain,
    Evidence,
    ScenarioSpec,
    TraceEvent,
    WorkerOutput,
    WorkerPayload,
)
from app.utils.logger import get_logger
from app.utils.parser import truncate

log = get_logger("engine_b.worker")

# 도구 조사 단계를 끝내고 최종 결론을 뽑을 때 붙이는 마지막 지시.
# 도구 호출과 구조화 출력을 한 번에 요구하면 모델이 조사를 건너뛰는 경향이 있어 단계를 나눴다.
_FINALIZE = (
    "조사를 마쳤다. 지금까지 확보한 근거만으로 최종 결론을 내라. "
    "확인하지 못한 내용은 추정임을 밝히고 confidence 를 낮춰라."
)


# ─── 워커 선언 ────────────────────────────────────────────────────

@dataclass(frozen=True)
class WorkerSpec:
    """워커 1개의 선언. 축마다 이것만 만들면 된다."""

    domain: Domain
    dimension: str
    role: str  # app/prompts/<dimension>.py 의 ROLE
    method: str  # app/prompts/<dimension>.py 의 METHOD
    result_model: type[BaseModel]  # result 필드의 스키마
    tools: tuple[BaseTool, ...] = ()
    # tools 와 달리 요청 시점에 비동기로 가져와야 하는 도구용 (예: gmail — MCP
    # 서버에 네트워크로 물어봐야 나온다). 모듈 로드 시점엔 값이 없어 tools 처럼
    # 정적 튜플로 못 넣는다 — run_worker() 가 실행 직전에 이걸 호출해 tools 와
    # 합친다. 반드시 읽기 전용 도구만 넣을 것: run_tool_loop() 은 LangGraph의
    # HumanInTheLoopMiddleware 같은 승인 게이트가 없어서, 쓰기 도구가 섞이면
    # 사람 승인 없이 그 즉시 실행돼 버린다(app/common/llm_client.py의
    # run_tool_loop()이 이를 이중으로 막는다 — is_write() 검사 참고).
    async_tools: Callable[[], Awaitable[Iterable[BaseTool]]] | None = None
    # 이 워커에게 보여줄 컨텍스트 섹션. 필요 없는 섹션을 빼서 토큰을 아낀다.
    context_sections: tuple[str, ...] = ALL_SECTIONS
    # 프롬프트 조립 대신 자체 구현을 쓰는 워커용. 담당자 3의 meeting 에이전트가 여기 붙는다.
    runner: Callable[["WorkerSpec", WorkerPayload], Awaitable[WorkerOutput]] | None = None

    @property
    def node_name(self) -> str:
        """LangGraph 노드 이름. 도메인이 달라도 축 이름이 겹치지 않게 접두사를 붙인다."""
        return f"{self.domain}.{self.dimension}"


# ─── 실행 ─────────────────────────────────────────────────────────

@lru_cache(maxsize=16)
def _envelope_model(result_model: type[BaseModel]) -> type[BaseModel]:
    """result + reasoning + confidence + evidence 공통 봉투를 축별 스키마로 만든다."""
    return create_model(
        f"{result_model.__name__}Envelope",
        result=(result_model, ...),
        reasoning=(str, ...),
        confidence=(float, ...),
        evidence=(list[Evidence], Field(default_factory=list)),
        __doc__="워커 공통 출력. reasoning 은 3~5문장, confidence 는 0.0~1.0.",
    )


async def run_worker(spec: WorkerSpec, payload: WorkerPayload) -> WorkerOutput:
    """워커 1개 실행. 실패해도 예외를 밖으로 던지지 않는다.

    워커 하나가 죽었다고 그래프 전체가 멈추면 나머지 4개 분석까지 버리게 된다.
    실패는 error 필드에 담고 confidence 0 으로 내려보내, 통합 단계에서 걸러지게 한다.
    """
    settings = get_settings()
    plan: AnalysisPlan = payload["plan"]
    context: AnalysisContext = payload["context"]
    scenario: ScenarioSpec = payload.get("scenario") or ScenarioSpec()
    feedback = list(payload.get("feedback") or [])
    attempt = int(payload.get("attempt") or 1)

    component = f"engine_b.worker.{spec.dimension}"
    log.info(
        "%s 시작 (scenario=%s, attempt=%d, 재작성지시 %d건)",
        spec.dimension,
        scenario.scenario_id,
        attempt,
        len(feedback),
    )

    system = compose_worker_system(
        role=spec.role,
        method=spec.method,
        dimension=spec.dimension,
        focus=plan.focus,
        max_tool_calls=settings.worker_max_tool_calls,
    )
    messages = [
        SystemMessage(content=system),
        HumanMessage(content=_render_task(spec, plan, context, scenario, feedback)),
    ]

    traces: list[llm_client.ToolCallTrace] = []
    try:
        tools = list(spec.tools)
        if spec.async_tools:
            try:
                tools += list(await spec.async_tools())
            except Exception as exc:  # noqa: BLE001 — 부가 도구 로딩 실패로 워커 전체를 죽이지 않는다
                log.warning("%s: async_tools 로딩 실패, 정적 도구만으로 진행: %s", spec.dimension, exc)

        if tools:
            loop = await llm_client.run_tool_loop(
                messages,
                tools,
                component=component,
                max_tool_calls=settings.worker_max_tool_calls,
            )
            messages, traces = loop.messages, loop.traces

        envelope = await llm_client.structured_call(
            [*messages, HumanMessage(content=_FINALIZE)],
            _envelope_model(spec.result_model),
            component=component,
        )
    except Exception as exc:
        log.error("%s 실패: %s", spec.dimension, exc)
        return WorkerOutput(
            dimension=spec.dimension,
            domain=spec.domain,
            scenario_id=scenario.scenario_id,
            attempt=attempt,
            confidence=0.0,
            reasoning=f"분석에 실패했다: {exc}",
            error=str(exc),
            tool_calls=len(traces),
        )

    evidence = list(envelope.evidence)
    evidence += _tool_evidence(traces, evidence)

    output = WorkerOutput(
        dimension=spec.dimension,
        domain=spec.domain,
        result=envelope.result.model_dump(mode="json"),
        reasoning=envelope.reasoning,
        confidence=min(1.0, max(0.0, envelope.confidence)),
        evidence=evidence,
        scenario_id=scenario.scenario_id,
        tool_calls=len(traces),
        attempt=attempt,
    )
    log.info(
        "%s 완료 (conf=%.2f, 도구 %d회)", spec.dimension, output.confidence, output.tool_calls
    )
    return output


def make_node(spec: WorkerSpec):
    """LangGraph 노드 함수를 만든다. Send() 로 받은 payload 를 그대로 처리한다."""

    async def node(payload: WorkerPayload) -> dict:
        run = spec.runner or run_worker
        result = await run(spec, payload)
        # 자체 구현 워커는 축을 여러 개 내놓을 수 있다 (meeting 이 슬롯+적합도 2개).
        outputs = result if isinstance(result, list) else [result]
        return {
            "worker_outputs": outputs,
            "trace": [
                TraceEvent(
                    node=spec.node_name,
                    message=truncate(output.reasoning, 200),
                    payload={
                        "confidence": output.confidence,
                        "tool_calls": output.tool_calls,
                        "attempt": output.attempt,
                        "scenario_id": output.scenario_id,
                        "error": output.error,
                    },
                )
                for output in outputs
            ],
        }

    node.__name__ = spec.node_name.replace(".", "_")
    return node


# ─── 프롬프트 조립 ────────────────────────────────────────────────

def _render_task(
    spec: WorkerSpec,
    plan: AnalysisPlan,
    context: AnalysisContext,
    scenario: ScenarioSpec,
    feedback: list[str],
) -> str:
    constraints = (
        "\n".join(f"- {item}" for item in plan.constraints) if plan.constraints else "- (없음)"
    )
    return (
        f"[분석 목표]\n{plan.objective}\n\n"
        f"[사용자가 못 박은 제약]\n{constraints}\n\n"
        f"[컨텍스트]\n{render_context(context, spec.context_sections, scenario)}\n"
        f"{feedback_block(feedback)}\n"
        f"위 정보를 바탕으로 '{spec.dimension}' 축을 분석하라."
    )


def _tool_evidence(
    traces: list[llm_client.ToolCallTrace], existing: list[Evidence]
) -> list[Evidence]:
    """모델이 근거로 안 적은 도구 호출도 출처로 남긴다 (추적성 확보)."""
    cited = {item.ref for item in existing}
    extra = []
    for trace in traces:
        ref = f"tool:{trace.name}"
        if not trace.ok or ref in cited:
            continue
        cited.add(ref)
        extra.append(
            Evidence(source="tool", ref=ref, detail=truncate(trace.summary, 200))
        )
    return extra
