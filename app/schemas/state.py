"""
내부 State / 타입 정의  —  서버 내부에서만 도는 형식 (프론트는 볼 일 없음)

★ 담당자 1이 초안, 담당자 2·3과 함께 확정. 변경은 반드시 합의 후.

여기 있는 것:
  - 인증/권한 컨텍스트   (AuthContext)   ← Worker·Tool까지 전달
  - Worker 공통 출력      (WorkerOutput)  ← 모든 워커가 이 형식으로
  - Router 결정           (RouteDecision)
  - Validator 위반        (Violation)
  - Engine B 그래프 상태  (EngineBState)  ← LangGraph State

프론트와 주고받는 형식(요청/응답/제안/승인)은 request.py · response.py 참고.
기술 전제: FastAPI + LangGraph. Pydantic v2.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─── 공통 열거형 (request/response도 재사용) ────────────────
class Domain(str, Enum):
    project = "project"
    hcm = "hcm"              # 인력 배정
    meeting = "meeting"
    vacation = "vacation"
    expense = "expense"


class Mode(str, Enum):
    analysis = "analysis"        # 현재 상태 진단
    derivation = "derivation"    # 후보 생성 (회의 슬롯, 인력 추천)
    replan = "replan"            # 조정안 생성 + 재평가


class Route(str, Enum):
    simple_query = "simple_query"
    engine_a = "engine_a"
    engine_b = "engine_b"


# ─── 인증 컨텍스트 (담당자 1) ──────────────────────────────
#   백엔드가 인증을 끝낸 뒤 user_id를 넘겨준다 (채팅 API가 body로 전달).
#   AI 서버는 토큰 검증을 하지 않으므로 user_id만 담는다.
#   역할(PM/MEMBER) 기반 접근 분기 없음 — 프로젝트 생성·수정 등 전 사원 허용.
#   "이 프로젝트 멤버인가" 같은 소속 검증이 필요하면 백엔드에 위임.
class AuthContext(BaseModel):
    user_id: int


# ─── Worker 공통 출력 (★ 모든 워커가 이 형식) ──────────────
class WorkerOutput(BaseModel):
    dimension: str               # "priority" | "risk" | "cost" | "skill_fit" | "workload" ...
    result: dict[str, Any]       # 워커별 실제 판단 (구조는 dimension마다 다름)
    reasoning: str               # 판단 근거 (감사로그 보존)
    confidence: float = Field(ge=0.0, le=1.0)


# ─── Router 결정 ───────────────────────────────────────────
class RouteDecision(BaseModel):
    route: Route
    domain: Optional[Domain] = None
    mode: Optional[Mode] = None
    focus: Optional[str] = None  # 강조점. 실행과 무관 (Worker는 항상 전부 병렬)


# ─── Validator 위반 (코드 검증) ────────────────────────────
class Violation(BaseModel):
    code: str                    # "PAST_DATE" | "BUDGET_OVER" | "OVERLOAD" ...
    message: str
    ref: Optional[str] = None    # 위반 대상 (milestoneId, userId 등)


# ─── Engine B 그래프 상태 (LangGraph State) ────────────────
#     각 노드가 자기 필드만 채우고 다음 노드로 넘긴다 (단일책임).
class EngineBState(BaseModel):
    auth: AuthContext
    decision: RouteDecision
    context: dict[str, Any] = Field(default_factory=dict)               # Context Builder
    worker_outputs: list[WorkerOutput] = Field(default_factory=list)    # Worker Layer
    violations: list[Violation] = Field(default_factory=list)           # Validator
    synthesis: Optional[dict[str, Any]] = None                          # Synthesis
    scenarios: list[dict[str, Any]] = Field(default_factory=list)       # replan 시나리오
    recommendation: Optional[dict[str, Any]] = None                     # 최종 추천 → HITL
