"""
요청 형식  —  프론트 → 서버로 들어오는 body

Notion API 명세(프로젝트 도메인)와 1:1로 대응한다.
프론트 개발자는 이 파일만 보면 "뭘 보내야 하는지" 알 수 있어야 한다.

담당자 1이 공통/AI 요청 초안, 각 도메인 상세는 담당자가 채운다.
Pydantic v2.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.schemas.state import Domain, Mode


# ─── Spring → FastAPI 공통 요청 (연동 규격 §3) ─────────────
class MessageItem(BaseModel):
    role: Literal["USER", "AGENT"]
    content: str


class ScreenContext(BaseModel):
    screen: str                                    # 화면 식별자 (예: PROJECT_CREATE)
    formState: dict[str, Any] = Field(default_factory=dict)  # 화면 폼 현재 입력값


class AgentRequest(BaseModel):
    """Spring이 채워서 보내는 공통 요청. FE가 직접 보내지 않는다."""
    userId: int                                    # Spring이 JWT에서 꺼내 넣음
    conversationId: int
    goal: str                                      # 사용자 입력 원문
    messages: list[MessageItem] = Field(default_factory=list)  # 최근 10건
    screenContext: ScreenContext
    requestSource: str = "WEB"
    locale: str = "ko-KR"


# ─── AI 생성형: 인력 배정 추천 ─────────────────────────────
class StaffingRecommendationRequest(BaseModel):
    project_id: Optional[int] = None
    project_name: str
    description: Optional[str] = None
    start_date: str
    target_date: str
    required_skills: list[str] = Field(default_factory=list)
    headcount: int = 5


# ─── AI 생성형: 프로젝트 재계획 ────────────────────────────
class ReplanRequest(BaseModel):
    trigger: Optional[
        Literal["deadline_slip", "member_left", "budget_overrun", "scope_change"]
    ] = None
    context: Optional[str] = None
    scenario_count: int = 3


# ─── HITL 확정: 모든 생성형 기능의 승인/거절 (담당자1) ─────
class DecisionRequest(BaseModel):
    action: Literal["approve", "reject", "replan"]
    selection: dict[str, Any] = Field(default_factory=dict)
    rejection_reason: Optional[str] = None


# TODO (각 도메인 담당자):
#   - 회의록 작성/수정, 지출 CRUD, 마일스톤/Task CRUD 등 순수 CRUD 요청 모델
#   - AI 요약은 Query Parameter(scope)만 있어 별도 body 없음
