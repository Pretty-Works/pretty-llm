"""
프로젝트 도메인 API — 분석(엔진 B 직접 진입)

vacation과 달리 조회성 분석이라 HITL 없이 결과를 바로 반환한다.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.orchestrator.orchestrator import handle
from app.schemas.request import AgentRequest
from app.schemas.response import ApiResponse
from app.schemas.state import Domain

router = APIRouter(prefix="/api/v1/projects", tags=["project"])


@router.post("/analyze")
def analyze_project(req: AgentRequest):
    """프로젝트 분석 요청 → 엔진 B가 여러 관점 병렬 분석."""
    req.domain_hint = Domain.project     # 이 문으로 들어오면 project로 확정
    result = handle(req)
    return ApiResponse.ok(result)
