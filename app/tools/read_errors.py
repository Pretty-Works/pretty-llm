"""
조회 도구 공통 실패 복구 — write_exec 의 조회판

쓰기는 execute_write() 가 4xx/5xx 를 LLM 이 읽을 문장으로 바꿔 돌려주지만,
조회는 backend.get() 예외가 그대로 튀어 실행이 죽었다
(실사용 피드백: 조회 에러 → 곧바로 "요청을 처리하지 못했어요").

ToolNode 기본 처리(langgraph 1.2.9)는 인자 오류(ToolInvocationError)만 문장으로
돌려주고 도구 안에서 난 예외는 re-raise 한다 — 실측 확인. 그래서 도구마다
try/except 를 심는 대신 create_agent 미들웨어 한 곳에서 잡는다.

백엔드 예외 2종만 문장으로 바꾸고, 그 밖(코드 버그)은 그대로 전파해 실행을
멈춘다 — 버그를 LLM 에게 문장으로 주면 지어낸 답으로 덮인다.
HITL 인터럽트 같은 제어 신호는 이 미들웨어를 통과한다(langchain 보장).
"""

from __future__ import annotations

from langchain.agents.middleware import ToolErrorMiddleware

from app.common.exceptions import BackendUnavailableError, BackendValidationError


def _read_error_text(exc: Exception, request) -> str | None:
    if isinstance(exc, BackendValidationError):
        return (f"조회가 거부되었습니다({exc.backend_code or '4xx'}): {exc}. "
                "인자(ID·날짜 등)가 틀렸을 수 있습니다 — 다른 조회 도구로 실제 값을 "
                "확인해 고쳐서 다시 호출하고, 대상을 특정할 수 없으면 사용자에게 확인하세요.")
    if isinstance(exc, BackendUnavailableError):
        return (f"백엔드 일시 장애로 조회에 실패했습니다: {exc}. "
                "같은 호출을 한 번만 다시 시도하고, 또 실패하면 잠시 후 다시 시도해 "
                "달라고 안내하세요 — 데이터가 없는 것이라고 단정하지 마세요.")
    return None                      # 나머지는 전파 — 실행이 멈추는 게 맞다


def read_error_middleware() -> ToolErrorMiddleware:
    """조회 실패를 LLM 이 읽고 복구하게 하는 미들웨어. 에이전트마다 새로 만든다."""
    return ToolErrorMiddleware(_read_error_text)
