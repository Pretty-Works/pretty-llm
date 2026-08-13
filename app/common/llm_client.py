# app/common/llm_client.py
# LLM 호출 공통 계층: 모든 에이전트가 LLM을 직접 부르지 않고 여기를 거친다.
# 모델 선택·토큰 로깅·타임아웃·재시도·Tool 자율호출 루프를 한 곳에서 관리. (담당자1)
#
# NOTE(담당자2): Engine B 가 이 계층 없이는 돌지 않아서, 위 주석에 적힌 명세대로
# 우선 채워두었습니다. Engine A 쪽 요구사항에 맞게 자유롭게 확장/교체하세요.
# 아래 4개가 공개 API 입니다:
#   get_llm()          - 모델 인스턴스
#   invoke()           - 단발 호출 (토큰 로깅 포함)
#   structured_call()  - Pydantic 스키마로 강제 구조화 출력
#   run_tool_loop()    - 근거가 부족할 때 에이전트가 스스로 툴을 부르는 루프

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, Sequence, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger("common.llm_client")

Profile = Literal["worker", "reasoning"]
TModel = TypeVar("TModel", bound=BaseModel)


# ─── 자료구조 ─────────────────────────────────────────────────────

class LLMNotConfigured(RuntimeError):
    """OPENAI_API_KEY 가 없을 때. 임포트 시점이 아니라 호출 시점에 터뜨린다."""


@dataclass
class ToolCallTrace:
    """워커가 어떤 근거를 어떻게 가져왔는지 남기는 기록."""

    name: str
    args: dict[str, Any]
    ok: bool
    summary: str


@dataclass
class LLMUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    def add(self, message: BaseMessage) -> None:
        usage = getattr(message, "usage_metadata", None) or {}
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        self.calls += 1


# 프로세스 전체 누적. 비용 비교나 요청 단위 계측에 쓴다.
_total = LLMUsage()


def usage_snapshot() -> LLMUsage:
    return LLMUsage(_total.input_tokens, _total.output_tokens, _total.calls)


def reset_usage() -> None:
    _total.input_tokens = _total.output_tokens = _total.calls = 0


@dataclass
class ToolLoopResult:
    messages: list[BaseMessage] = field(default_factory=list)
    traces: list[ToolCallTrace] = field(default_factory=list)
    usage: LLMUsage = field(default_factory=LLMUsage)
    hit_limit: bool = False


# ─── 모델 인스턴스 ────────────────────────────────────────────────

@lru_cache(maxsize=8)
def get_llm(profile: Profile = "worker", temperature: float | None = None) -> BaseChatModel:
    """프로파일별 모델 인스턴스. 같은 설정이면 재사용된다.

    worker    - 호출 수가 많은 워커용 저비용 모델
    reasoning - 라우터/합성처럼 판단 품질이 중요한 곳
    """
    settings = get_settings()
    if not settings.llm_api_key:
        raise LLMNotConfigured(
            "LLM_API_KEY (또는 OPENAI_API_KEY) 가 설정되어 있지 않습니다. .env 를 확인하세요."
        )

    from langchain_openai import ChatOpenAI  # 지연 임포트: 키 없이도 모듈 임포트는 되게

    model = (
        settings.llm_model_reasoning if profile == "reasoning" else settings.llm_model_worker
    )
    return ChatOpenAI(
        model=model,
        temperature=settings.llm_temperature if temperature is None else temperature,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
        api_key=settings.llm_api_key,
    )


# ─── 단발 호출 ────────────────────────────────────────────────────

async def invoke(
    messages: Sequence[BaseMessage],
    *,
    profile: Profile = "worker",
    component: str = "unknown",
    temperature: float | None = None,
) -> AIMessage:
    """단발 호출 + 토큰 로깅."""
    llm = get_llm(profile, temperature)
    response = await llm.ainvoke(list(messages))
    _log_usage(component, response)
    return response  # type: ignore[return-value]


async def structured_call(
    messages: Sequence[BaseMessage],
    schema: type[TModel],
    *,
    profile: Profile = "worker",
    component: str = "unknown",
    temperature: float | None = None,
    method: str = "json_schema",
) -> TModel:
    """Pydantic 스키마로 출력 형태를 강제한다.

    파싱 실패를 애플리케이션 코드에서 다루지 않아도 되도록 구조화 출력만 쓴다.

    기본은 엄격 모드(json_schema)다. 중첩 스키마에서 모델이 필드를 헷갈리지 않는다.
    다만 `dict[str, Any]` 같은 자유형 필드가 있으면 엄격 모드가 거부하므로
    (SynthesisResult.proposed_changes) 그런 호출만 method="function_calling" 을 준다.

    include_raw 는 토큰 사용량을 남기기 위한 것 — 파싱된 객체만 받으면 계측이 안 된다.
    """
    llm = get_llm(profile, temperature)
    structured = llm.with_structured_output(schema, method=method, include_raw=True)
    payload = await structured.ainvoke(list(messages))

    raw, parsed = payload.get("raw"), payload.get("parsed")
    if raw is not None:
        _log_usage(component, raw)
    if parsed is None:
        raise ValueError(f"구조화 출력 파싱 실패: {payload.get('parsing_error')}")

    log.debug("[%s] structured_call -> %s", component, schema.__name__)
    return parsed  # type: ignore[return-value]


# ─── Tool 자율호출 루프 ───────────────────────────────────────────

async def run_tool_loop(
    messages: Sequence[BaseMessage],
    tools: Sequence[BaseTool],
    *,
    profile: Profile = "worker",
    component: str = "unknown",
    max_tool_calls: int | None = None,
) -> ToolLoopResult:
    """근거가 부족하면 에이전트가 스스로 툴을 호출하는 루프.

    LLM 이 더 이상 툴을 부르지 않거나 `max_tool_calls` 에 도달하면 종료한다.
    반환된 messages 를 그대로 이어 붙여 structured_call 로 최종 답을 뽑는 방식이다.
    (툴 호출과 구조화 출력을 한 번에 강제하면 모델이 툴을 건너뛰는 경향이 있어 2단계로 나눴다.)

    ★ 여기 들어오는 도구는 승인(HITL) 게이트가 없다 — LLM이 부르면 그 즉시
    실행된다(engine_a의 build_domain_agent()가 쓰는 HumanInTheLoopMiddleware와
    다르다). registry.is_write() 가 True 인 쓰기 도구가 하나라도 섞여 들어오면
    여기서 바로 거부한다 — 호출자(예: 워커의 async_tools)가 필터링을 깜빡했을
    때의 백스톱이다. 1차 방어는 도구를 만드는 쪽(예: gmail_mcp_client의
    get_gmail_read_tools())이 화이트리스트로 거르는 것이고, 이건 그게 뚫렸을
    때의 2차 방어선이다.
    """
    from app.tools.registry import is_write

    unsafe = [t.name for t in tools if is_write(t.name)]
    if unsafe:
        raise RuntimeError(
            f"run_tool_loop 에 승인 게이트 없는 쓰기 도구가 섞여 들어옴: {unsafe} "
            "— 이 루프는 도구를 부르는 즉시 실행하므로 쓰기 도구를 넣으면 안 된다."
        )

    settings = get_settings()
    budget = settings.worker_max_tool_calls if max_tool_calls is None else max_tool_calls

    result = ToolLoopResult(messages=list(messages))
    if not tools or budget <= 0:
        return result

    llm = get_llm(profile).bind_tools(list(tools))
    by_name = {tool.name: tool for tool in tools}
    used = 0

    while used < budget:
        response: AIMessage = await llm.ainvoke(result.messages)  # type: ignore[assignment]
        result.usage.add(response)
        _log_usage(component, response)
        result.messages.append(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            return result

        for call in tool_calls:
            if used >= budget:
                # 예산을 넘긴 호출은 실행하지 않되, 모델이 기다리지 않도록 응답은 채워준다.
                result.messages.append(
                    ToolMessage(
                        content="도구 호출 한도를 초과했습니다. 지금까지 확보한 근거로 답하세요.",
                        tool_call_id=call["id"],
                    )
                )
                result.hit_limit = True
                continue

            used += 1
            name, args = call["name"], call.get("args") or {}
            trace = await _execute_tool(by_name, name, args)
            result.traces.append(trace)
            result.messages.append(
                ToolMessage(content=trace.summary, tool_call_id=call["id"])
            )
            log.info("[%s] tool %s(%s) ok=%s", component, name, args, trace.ok)

    result.hit_limit = True
    log.info("[%s] 툴 호출 한도(%d) 도달", component, budget)
    return result


# ─── 내부 헬퍼 ────────────────────────────────────────────────────

async def _execute_tool(
    by_name: dict[str, BaseTool], name: str, args: dict[str, Any]
) -> ToolCallTrace:
    tool = by_name.get(name)
    if tool is None:
        return ToolCallTrace(name, args, False, f"알 수 없는 도구입니다: {name}")
    try:
        output = await tool.ainvoke(args)
    except Exception as exc:  # 툴 실패로 그래프 전체가 죽지 않게 한다
        log.warning("tool %s 실패: %s", name, exc)
        return ToolCallTrace(name, args, False, f"도구 실행 실패: {exc}")
    return ToolCallTrace(name, args, True, str(output))


def _log_usage(component: str, message: BaseMessage) -> None:
    _total.add(message)
    usage = getattr(message, "usage_metadata", None)
    if usage:
        # ★ 2026-08-13 — total_tokens 추가. TPM 한도(429 원인)는 in+out 합계로
        #   깎이므로, 호출 하나가 한도에 얼마나 부담을 주는지 보려면 합계가 필요하다.
        in_tok, out_tok = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
        log.info(
            "[%s] tokens in=%s out=%s total=%s (누적 calls=%s total=%s)",
            component, in_tok, out_tok, in_tok + out_tok,
            _total.calls, _total.input_tokens + _total.output_tokens,
        )
