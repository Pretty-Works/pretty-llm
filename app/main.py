"""
서버 시작 스위치

FastAPI 앱을 만들고, api/ 라우터를 연결하고, 공통 규칙(예외 핸들러 등)을 등록한다.
실행:  uvicorn app.main:app --reload

담당자 1.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.common.auth import verify_internal_api_key
from app.config import settings

# BE(스프링)만 이 서버를 부를 수 있어야 하는 라우터 전부에 공통으로 건다 —
# app/common/auth.py 참고. /health 는 배포/모니터링이 찌르는 엔드포인트라
# 여기 포함하지 않는다(아래 include_router 들과 별개로 app.get 으로 직접 등록).
_INTERNAL_ONLY = [Depends(verify_internal_api_key)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[startup] app={settings.app_name} debug={settings.debug}")
    yield
    # sqlite 커넥션(체크포인트·기억 카드)을 닫는다. aiosqlite 는 커넥션마다
    # 비데몬 스레드를 띄우므로, 안 닫으면 파이썬이 종료를 못 하고 컨테이너
    # 중지 때 유예시간을 다 쓴 뒤 강제 종료된다.
    from app.common.checkpoint import close_checkpointer
    from app.memory.store import close_memory_store

    await close_checkpointer()
    await close_memory_store()
    print("[shutdown] cleanup done")


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 프로덕션에서는 프론트 도메인으로 교체
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
app.include_router(router, prefix="/api/v1", dependencies=_INTERNAL_ONLY)

# 에이전트 전용 API (규격 v2, SSE) — /api/v1 과 별개 계약이라 prefix 없이 등록
from app.api import agent as agent_api  # noqa: E402
from app.api import docs as docs_api  # noqa: E402

app.include_router(agent_api.router, dependencies=_INTERNAL_ONLY)
app.include_router(docs_api.router, dependencies=_INTERNAL_ONLY)

# 배치용 탭 요약 생성 API (BE→LLM, 단발 JSON) — 역시 /api/v1 밖 계약이라 prefix 없이 등록
from app.api.project import agent_router as project_summary_router  # noqa: E402

app.include_router(project_summary_router, dependencies=_INTERNAL_ONLY)

# 회의록 초안 생성 API (BE→LLM, 단발 JSON) — txt 업로드 → 작성 화면 폼 초안
from app.api.meeting import agent_router as meeting_draft_router  # noqa: E402

app.include_router(meeting_draft_router, dependencies=_INTERNAL_ONLY)

# 에이전트 패널 추천 문구 (BE→LLM, 단발 JSON) — 화면 진입 시 보여줄 3개
from app.api.suggestions import agent_router as suggestions_router  # noqa: E402

app.include_router(suggestions_router, dependencies=_INTERNAL_ONLY)


# 글로벌 예외 핸들러
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "errorCode": "INTERNAL_ERROR",
            "message": str(exc),
            "result": None,
        },
    )


@app.get("/health")
def health() -> dict:
    """서버 생존 확인용. 배포/모니터링에서 이걸 찌른다."""
    return {"status": "ok", "app": settings.app_name}
