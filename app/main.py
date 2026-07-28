"""
서버 시작 스위치

FastAPI 앱을 만들고, api/ 라우터를 연결하고, 공통 규칙(예외 핸들러 등)을 등록한다.
실행:  uvicorn app.main:app --reload

담당자 1.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.config import settings

app = FastAPI(title=settings.app_name)


@app.get("/health")
def health() -> dict:
    """서버 생존 확인용. 배포/모니터링에서 이걸 찌른다."""
    return {"status": "ok", "app": settings.app_name}


# TODO (다음 단계)
#   from app.api import routes
#   app.include_router(routes.router)              # api/ 의 엔드포인트 연결
#
#   from app.common.exceptions import register_exception_handlers
#   register_exception_handlers(app)               # 503·422·429 등 공통 에러 응답
