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


# ─── 진입: 에이전트에게 보내는 자연어 요청 ─────────────────
class AgentRequest(BaseModel):
    """Orchestrator 진입점. 대부분의 AI 기능이 여기로 들어온다."""
    message: str                              # 사용자 자연어
    domain_hint: Optional[Domain] = None      # 화면에서 이미 아는 경우 힌트
    context: dict[str, Any] = Field(default_factory=dict)  # 현재 화면 맥락(폼 draft 등)


# ─── AI 생성형: 인력 배정 추천 ─────────────────────────────
class StaffingRecommendationRequest(BaseModel):
    project_id: Optional[int] = None          # 있으면 기존 프로젝트 인원 추가 모드
    project_name: str
    description: Optional[str] = None          # 미입력 시 에이전트가 필요 역량 추론
    start_date: str
    target_date: str
    required_skills: list[str] = Field(default_factory=list)
    headcount: int = 5


# ─── AI 생성형: 프로젝트 재계획 ────────────────────────────
class ReplanRequest(BaseModel):
    trigger: Optional[
        Literal["deadline_slip", "member_left", "budget_overrun", "scope_change"]
    ] = None                                   # None이면 서버가 자체 진단
    context: Optional[str] = None              # 사용자 보충 설명
    scenario_count: int = 3


# ─── HITL 확정: 모든 생성형 기능의 승인/거절 (담당자 1) ────
#     approve/reject/replan 공통. selection 키는 기능마다 다름.
class DecisionRequest(BaseModel):
    action: Literal["approve", "reject", "replan"]
    selection: dict[str, Any] = Field(default_factory=dict)
    # 예) 인력배정 → {"selectedUserIds": [5, 9]}
    #     재계획   → {"selectedScenarioId": "sc_2", "appliedScopes": ["milestones","budget"]}
    rejection_reason: Optional[str] = None     # action="replan"일 때 필수


# TODO (각 도메인 담당자):
#   - 회의록 작성/수정, 지출 CRUD, 마일스톤/Task CRUD 등 순수 CRUD 요청 모델
#   - AI 요약은 Query Parameter(scope)만 있어 별도 body 없음
