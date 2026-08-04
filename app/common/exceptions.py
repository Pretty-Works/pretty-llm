"""
공통 예외 + 에러 응답 핸들러

목적:
    워커/엔진 어디서 터지든, 프론트에는 항상 같은 형식 {errorCode, message, result}
    으로 에러가 나가게 한다. 각 파일에서 raise 만 하면 여기서 응답 형태로 변환.

우리 서비스의 AI 고유 실패 케이스 (API 명세에서 정의한 것):
    - LLM 타임아웃/호출 실패        → 503
    - 근거 데이터 부족(분석 불가)    → 422
    - replan 재시도 횟수 초과        → 429

담당자 1. FastAPI exception handler.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


# ─────────────────────────────────────────────────────────────
# 1. 커스텀 예외들
#    - 코드 어디서든 `raise LLMTimeoutError()` 처럼 던지기만 하면 된다.
#    - status_code / error_code 는 각 예외가 스스로 안다.
# ─────────────────────────────────────────────────────────────
class AppError(Exception):
    """모든 커스텀 예외의 부모. 공통 필드(status_code, error_code, message)를 갖는다."""
    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"

    def __init__(self, message: str | None = None):
        # TODO: message 가 없으면 기본 메시지를 쓰도록 처리
        self.message = message or self.__class__.__name__
        super().__init__(self.message)


class LLMTimeoutError(AppError):
    """LLM 호출이 타임아웃/실패했을 때."""
    status_code = 503
    error_code = "AI_503"


class InsufficientDataError(AppError):
    """분석/추천에 필요한 근거 데이터가 부족할 때 (예: 사원 스킬 미등록)."""
    status_code = 422
    error_code = "AI_422"


class RetryLimitError(AppError):
    """replan 재시도 횟수를 초과했을 때."""
    status_code = 429
    error_code = "AI_429"


# ─────────────────────────────────────────────────────────────
# 2. 예외 → 응답 변환 핸들러
#    - AppError 를 잡아서 {errorCode, message, result:null} 형태로 반환.
# ─────────────────────────────────────────────────────────────
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    # TODO:
    #   - status_code = exc.status_code
    #   - content = { "errorCode": exc.error_code, "message": exc.message, "result": None }
    #   - JSONResponse 로 반환
    #   (response.py 의 ApiResponse.fail 과 형식이 같아야 함)
    raise NotImplementedError


def register_exception_handlers(app: FastAPI) -> None:
    """main.py 에서 호출해 앱에 핸들러를 등록한다."""
    # TODO: app.add_exception_handler(AppError, app_error_handler)
    #   필요하면 FastAPI 기본 RequestValidationError(422) 도 같은 형식으로 감싸기
    raise NotImplementedError
