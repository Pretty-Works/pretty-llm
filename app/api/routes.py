"""
라우터 집합 — 모든 도메인 라우터를 한곳에 모아 main.py에 연결.

새 도메인 API가 생기면 여기에 include_router 한 줄 추가. (매핑 방식과 같은 원리)
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import project, vacation

router = APIRouter()
router.include_router(vacation.router)
router.include_router(project.router)
# router.include_router(meeting.router)    ← 다음
