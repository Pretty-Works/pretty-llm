# app/clients/gmail_mcp_client.py
"""Agent(이 프로세스)가 Gmail MCP 서버의 툴을 가져다 쓰는 창구.

⑧ Agent는 토큰도 user_id도 전혀 모른다 — 여기서 하는 일은 MCP 서버 URL에 붙어
tool 목록을 받아오는 것뿐이다. 인증/토큰/리프레시/user_id 역산은 전부
mcp_servers/gmail_mcp/ 쪽 책임이고, 이 파일은 그 존재조차 모른다.

★ run_id 를 LLM에게서 지킨다 — gmail-mcp 가 노출하는 tool 들은 MCP 프로토콜상
run_id 를 인자로 받게 돼 있다(mcp_tools.py). 이걸 그대로 bind_tools() 에 흘리면
LLM 이 tool_call.args 로 run_id 를 직접 채워 넣을 수 있는 스키마가 되고, 프롬프트
인젝션으로 "run_id=다른값" 을 넣어 다른 대화의 Gmail을 건드리는 경로가 열린다.
그래서 _lock_run_id() 가 MCP가 돌려준 tool 을 그대로 안 쓰고, run_id 인자를 실행
직전에 RunContext(app/common/run_context.py) 의 현재 run_id 로 강제로 덮어쓴
새 tool 로 감싸서 돌려준다. 이게 "BE는 최소, LLM 실행 레이어에서 최대한 처리"
원칙의 실행부다 — 새 API 없이 이 파일 안에서 끝난다.

★ 검증 메모(langchain-core==1.5.1, langchain-mcp-adapters==0.3.0 소스 직접 확인,
  pyproject.toml/uv.lock 고정 버전과 동일 — 다른 버전으로 올리면 재확인 필요):
  - langchain_mcp_adapters.tools.convert_mcp_tool_to_langchain_tool() 이 만드는
    args_schema는 pydantic 모델이 아니라 tool.inputSchema, 즉 **raw JSON schema
    dict** 다. dict 타입 args_schema에 대해서는 langchain_core.tools.base.
    BaseTool._parse_input() 이 pydantic 검증(=알 수 없는 필드 자동 제거)을 **안 한다**
    — dict 스키마일 땐 tool_input을 그대로 통과시킨다. 즉 "LLM 스키마에서
    run_id를 지운다"는 LLM이 그 인자를 볼 확률을 낮추는 것일 뿐, 그것만으로는
    LLM이 tool_call.args에 run_id를 몰래 끼워 넣는 걸 막지 못한다 — 그래서
    `_forced()`가 kwargs["run_id"]를 무조건 덮어쓰는 게 UX가 아니라 진짜
    유일한 방어선이다.
  - MCP tool의 coroutine(`call_tool`)은 `response_format="content_and_artifact"`
    를 전제로 (content, artifact) 2-tuple을 반환한다. 이 설정 없이 새
    StructuredTool을 처음부터 다시 만들면(name/description/args_schema/coroutine
    만 채워서) response_format이 기본값 "content"로 떨어지고, BaseTool이 그
    tuple을 통째로 content 취급해버려 결과가 깨진다(langchain_core/tools/base.py
    의 response_format 분기 참고). 그래서 새로 만들지 않고 `tool.model_copy(
    update={...})`로 args_schema/coroutine만 바꾼다 — response_format,
    metadata, handle_tool_error 등 나머지 필드가 전부 그대로 보존된다.

사용처: engine_a/domain_agents.py 같은 곳에서 다른 LangChain 툴들과 나란히
bind_tools() 에 섞어 쓴다. Gmail 관련 요청이 오면 LLM이 이 중 하나를 고른다.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, create_model

from app.common.run_context import current_run_id
from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger("clients.gmail_mcp_client")

_RUN_ID_FIELD = "run_id"


def _describe_exception(exc: BaseException, *, _depth: int = 0) -> str:
    """(Base)ExceptionGroup(anyio TaskGroup 에러 묶음)은 str()이 "unhandled errors
    in a TaskGroup (N sub-exceptions)"처럼 껍데기 요약만 준다 — 진짜 원인(연결
    거부·DNS 실패·421 Host 헤더 거부 등)은 .exceptions 안의 실제 예외에 있는데
    거기까지 안 펼치면 로그로는 원인을 못 찾는다(2026-08-13 실제로 겪음). 재귀로
    펼쳐서 타입+메시지를 이어붙인다 — TaskGroup 안에 TaskGroup이 또 있을 수 있어서
    depth 제한을 둔다."""
    prefix = "  " * _depth
    line = f"{prefix}{type(exc).__name__}: {exc}"
    sub_exceptions = getattr(exc, "exceptions", None)  # ExceptionGroup / BaseExceptionGroup 전용 속성
    if not sub_exceptions or _depth >= 5:
        return line
    children = "\n".join(_describe_exception(e, _depth=_depth + 1) for e in sub_exceptions)
    return f"{line}\n{children}"


@lru_cache(maxsize=1)
def _client() -> MultiServerMCPClient:
    settings = get_settings()
    return MultiServerMCPClient(
        {
            "gmail": {
                # 끝의 슬래시 필수 — gmail-mcp 쪽 mount("/mcp", ...) + 내부 route("/") 조합이라
                # 슬래시 없이 치면 307 리다이렉트가 한 번 더 낀다.
                "url": f"{settings.gmail_mcp_server_url.rstrip('/')}/mcp/",
                "transport": "streamable_http",
            }
        }
    )


def _strip_run_id_from_schema(schema: Any) -> Any:
    """(best-effort) LLM에게 보여줄 args_schema에서 run_id 필드를 뺀다.

    실제로 gmail-mcp 툴이 오는 경로(langchain-mcp-adapters)에서는 dict(raw JSON
    schema) 브랜치만 탄다 — pydantic 모델 브랜치는 다른 경로로 tool이 만들어질
    가능성에 대비한 방어적 코드다. 어느 쪽이든 이건 "LLM이 그 인자를 덜 보게"
    하는 UX용이고, 진짜 방어는 _lock_run_id()의 실행 시점 강제 주입이다(위 모듈
    docstring의 검증 메모 참고).
    """
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        fields = {
            name: (info.annotation, info)
            for name, info in schema.model_fields.items()
            if name != _RUN_ID_FIELD
        }
        return create_model(f"{schema.__name__}NoRunId", **fields)

    if isinstance(schema, dict):
        props = dict(schema.get("properties", {}))
        props.pop(_RUN_ID_FIELD, None)
        required = [r for r in schema.get("required", []) if r != _RUN_ID_FIELD]
        return {**schema, "properties": props, "required": required}

    # 모르는 형태면 원본 그대로 반환 — _lock_run_id() 의 강제 주입이 여전히 지켜준다.
    return schema


def _lock_run_id(tool: StructuredTool) -> StructuredTool | None:
    """gmail tool 하나를 run_id-안전 버전으로 바꾼다.

    실행 직전에 kwargs["run_id"] 를 RunContext 의 현재 run_id 로 무조건 덮어쓴다.
    LLM이 뭘 보냈든(심지어 run_id 를 안 보냈어도, 혹은 스키마에 없는데도 몰래
    끼워 넣었어도) 최종적으로 쓰이는 값은 항상 서버가 쥐고 있는 진짜 run_id
    하나뿐이다.

    ★ `StructuredTool(...)`로 처음부터 새로 만들지 않고 `tool.model_copy(update=...)`
    로 args_schema/coroutine만 바꾼다 — response_format="content_and_artifact"
    등 나머지 필드를 잃어버리면 MCP tool의 (content, artifact) 튜플 반환값이
    깨진다(위 모듈 docstring 검증 메모 참고).

    래핑 자체가 실패하면(예: 예상 못한 tool 형태) 그 tool 은 아예 반환 목록에서
    빼버린다 — "안전하게 못 감쌀 거면 노출하지 않는다"가 기본값이어야 한다. Gmail
    쪽 구멍보다는 그 tool 하나가 잠깐 안 보이는 쪽이 훨씬 낫다.
    """
    original_coroutine = tool.coroutine
    if original_coroutine is None:
        log.error("gmail tool(%s)에 coroutine이 없음 — run_id 강제 주입 불가, 이 tool은 제외", tool.name)
        return None

    async def _forced(**kwargs: Any) -> Any:
        run_id = current_run_id.get()
        if not run_id:
            log.warning("gmail tool(%s) 호출인데 current_run_id 없음 — 대화 세션 밖 호출 추정", tool.name)
            return {"connected": False, "error": "no_active_run"}
        kwargs[_RUN_ID_FIELD] = run_id  # LLM이 뭘 넣었든 여기서 무조건 덮어씀 — 진짜 방어선
        return await original_coroutine(**kwargs)

    try:
        new_schema = _strip_run_id_from_schema(tool.args_schema)
    except Exception as exc:  # noqa: BLE001 — 스키마 숨기기 실패는 치명적이지 않음
        log.warning("gmail tool(%s) args_schema에서 run_id 숨기기 실패(기능엔 지장 없음): %s", tool.name, exc)
        new_schema = tool.args_schema

    try:
        return tool.model_copy(update={"args_schema": new_schema, "coroutine": _forced})
    except Exception as exc:  # noqa: BLE001 — model_copy 자체가 실패하는 langchain-core 버전 대비
        log.error("gmail tool(%s) run_id 잠금 실패 — 이 tool은 노출하지 않음: %s", tool.name, exc)
        return None


async def get_gmail_tools() -> list:
    """LangChain Tool 객체 리스트. gmail_search_emails / gmail_get_email /
    gmail_send_email / gmail_connection_status 가 여기 담겨 온다.

    각 tool은 _lock_run_id()를 거쳐 run_id 인자가 LLM 스키마에서 빠지고 실행 시점에
    RunContext에서 강제 주입되는 버전으로만 반환된다.

    MCP 서버가 꺼져 있어도 Agent 전체가 죽지 않게 예외를 삼키고 빈 리스트로 폴백한다
    — Gmail 연동은 부가 기능이라 필수 경로를 막으면 안 된다.
    """
    try:
        raw_tools = await _client().get_tools(server_name="gmail")
    except Exception as exc:  # noqa: BLE001 — MCP 서버 다운 등 어떤 이유든 폴백
        log.warning(
            "gmail MCP 서버 연결 실패, gmail 툴 없이 진행:\n%s",
            _describe_exception(exc),
        )
        return []

    locked = [_lock_run_id(t) for t in raw_tools]
    return [t for t in locked if t is not None]


# 승인 게이트가 없는 실행 경로(app/common/llm_client.py의 run_tool_loop — 엔진B
# 워커가 근거 확보용으로 스스로 도구를 호출하는 루프)에 노출해도 안전한 도구만.
# ★ 화이트리스트다(gmail_send_email 만 빼는 블랙리스트가 아니다) — gmail-mcp 쪽에
# 새 쓰기 도구가 생겨도 여기 이름을 직접 추가하기 전까진 조용히 새어 들어오지
# 않는다("빠뜨리는 사고 원천 차단" — app/tools/registry.py의 is_write() 와 같은 방향).
_READ_ONLY_TOOL_NAMES = frozenset({
    "gmail_search_emails",
    "gmail_get_email",
    "gmail_connection_status",
})


async def get_gmail_read_tools() -> list:
    """get_gmail_tools() 의 읽기 전용 부분집합.

    build_domain_agent() 로 만든 에이전트(엔진A의 mail 도메인, 엔진B의
    replan_agent)는 HumanInTheLoopMiddleware 가 쓰기 도구를 자동으로 승인
    게이트 뒤에 두지만, run_tool_loop() 은 그런 게이트가 없어 도구를 부르는
    즉시 실행한다. 그 경로에 gmail_send_email 이 섞이면 분석 중인 LLM이 사람
    승인 없이 실제 메일을 보내버릴 수 있다 — 그래서 이 함수는 절대
    gmail_send_email 을 포함하지 않는다(app/common/llm_client.py의
    run_tool_loop() 이 이를 이중으로 강제한다).
    """
    tools = await get_gmail_tools()
    return [t for t in tools if t.name in _READ_ONLY_TOOL_NAMES]
