"""
내부 State / 타입 정의  —  서버 내부에서만 도는 형식 (프론트는 볼 일 없음)

★ 담당자 1이 초안, 담당자 2·3과 함께 확정. 변경은 반드시 합의 후.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ─── 공통 열거형 ────────────────────────────────────────────
class Domain(str, Enum):
    project = "project"
    hcm = "hcm"
    meeting = "meeting"
    vacation = "vacation"
    expense = "expense"


class Mode(str, Enum):
    analysis = "analysis"
    derivation = "derivation"
    replan = "replan"


class Route(str, Enum):
    simple_query = "simple_query"
    engine_a = "engine_a"
    engine_b = "engine_b"


# ─── 인증 컨텍스트 (담당자1) ────────────────────────────────
class AuthContext(BaseModel):
    user_id: int


# ─── Worker 공통 출력 (모든 워커가 이 형식) ─────────────────
class WorkerOutput(BaseModel):
    dimension: str
    result: dict[str, Any]
    reasoning: str
    confidence: float = Field(ge=0.0, le=1.0)


# 하위 호환용 alias (기존 코드에서 AgentOutput으로 쓰던 것)
AgentOutput = WorkerOutput


# ─── Router 결정 ────────────────────────────────────────────
class RouteDecision(BaseModel):
    route: Route
    domain: Optional[Domain] = None
    mode: Optional[Mode] = None
    focus: Optional[str] = None


# ─── Validator 위반 ──────────────────────────────────────────
class Violation(BaseModel):
    code: str
    message: str
    ref: Optional[str] = None


# ─── Engine B 그래프 상태 (LangGraph State) ─────────────────
class EngineBState(BaseModel):
    auth: AuthContext
    decision: RouteDecision
    context: dict[str, Any] = Field(default_factory=dict)
    worker_outputs: list[WorkerOutput] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    synthesis: Optional[dict[str, Any]] = None
    scenarios: list[dict[str, Any]] = Field(default_factory=list)
    recommendation: Optional[dict[str, Any]] = None


# ─── Schedule Agent (담당자3) ────────────────────────────────
class ScheduleAgentInput(BaseModel):
    participant_ids: list[int]
    project_id: int
    duration_minutes: int
    from_date: str                  # YYYY-MM-DD
    to_date: str                    # YYYY-MM-DD


class MeetingSlot(BaseModel):
    start: str                      # ISO 8601
    end: str
    reason: str


class ScheduleAgentResult(BaseModel):
    slots: list[MeetingSlot]        # 최대 3개


# ─── Replanning (담당자3) ────────────────────────────────────
class ReplanningInput(BaseModel):
    project_id: int
    problem: str


class ScenarioResult(BaseModel):
    scenario_type: str              # 일정연장 / 인력추가 / 범위축소
    changes: list[str]
    expected_outcome: str
    risks: list[str]
    agent_outputs: list[WorkerOutput]


class ScenarioExecutorOutput(BaseModel):
    project_id: int
    problem: str
    scenarios: list[ScenarioResult]  # 항상 3개
