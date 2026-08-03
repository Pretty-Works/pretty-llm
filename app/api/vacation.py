"""
연차 도메인 API — create_agent + HITL 미들웨어 방식

1차) POST /approve         : 에이전트 실행 → 승인 필요 시 thread_id + 대기작업 반환
2차) POST /{thread}/decision: 사용자 승인/거절 → Command(resume)로 재개

★ user_id 는 body 로 들어온 값(백엔드가 인증 후 넣어줌)을 AuthContext 로 감싸
  context 로 전달한다. 프롬프트에 문자로 끼워넣지 않는다.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.common import hitl
from app.engine_a.vacation_agent import get_agent
from app.schemas.request import AgentRequest, DecisionRequest
from app.schemas.response import ApiResponse
from app.schemas.state import AuthContext

router = APIRouter(prefix="/api/v1/vacation", tags=["vacation"])


@router.post("/approve")
def approve_vacation(req: AgentRequest):
    """연차 요청 → 에이전트가 영향 분석 후 승인 도구 호출 직전에 멈춤."""
    thread_id = f"vac_{uuid.uuid4().hex[:8]}"
    auth = AuthContext(user_id=req.user_id)
    result = hitl.start(get_agent(), req.message, thread_id, auth)
    return ApiResponse.ok(result)


@router.post("/{thread_id}/decision")
def decide_vacation(thread_id: str, decision: DecisionRequest):
    """사용자 승인/거절 확정 → 멈춘 지점에서 재개.

    거절 시 rejection_reason 을 미들웨어에 실어 보내 에이전트가 재제안하게 한다.
    """
    result = hitl.resume(
        get_agent(), decision.action, thread_id, decision.rejection_reason
    )
    return ApiResponse.ok(result)
