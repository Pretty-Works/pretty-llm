from typing import Any, Optional
from pydantic import BaseModel


# ── 공통 Agent 출력 ───────────────────────────────────────────
class AgentOutput(BaseModel):
    dimension: str                  # 어떤 관점인지 (예: "schedule", "project_fit")
    result: Any                     # 실제 결과값
    reasoning: str                  # 판단 근거
    confidence: float               # 0.0 ~ 1.0


# ── Schedule Agent ────────────────────────────────────────────
class ScheduleAgentInput(BaseModel):
    participant_ids: list[int]      # 참가자 user ID 목록
    project_id: int                 # 관련 프로젝트 ID
    duration_minutes: int           # 회의 시간 (분)
    from_date: str                  # 탐색 시작일 (YYYY-MM-DD)
    to_date: str                    # 탐색 종료일 (YYYY-MM-DD)


class MeetingSlot(BaseModel):
    start: str                      # ISO 8601 (예: 2026-07-30T14:00:00)
    end: str
    reason: str                     # 이 슬롯을 추천한 이유


class ScheduleAgentResult(BaseModel):
    slots: list[MeetingSlot]        # 추천 후보 최대 3개


# ── Replanning ────────────────────────────────────────────────
class ReplanningInput(BaseModel):
    project_id: int
    problem: str                    # 문제 상황 설명 (예: "백엔드 개발 2주 지연")


class ScenarioType(str):
    EXTEND = "일정연장"
    ADD_RESOURCE = "인력추가"
    REDUCE_SCOPE = "범위축소"


class ScenarioResult(BaseModel):
    scenario_type: str              # 일정연장 / 인력추가 / 범위축소
    changes: list[str]              # 변경 내용 목록
    expected_outcome: str           # 예상 결과
    risks: list[str]                # 예상 리스크
    agent_outputs: list[AgentOutput] # 내부 에이전트 판단 결과


class ScenarioExecutorOutput(BaseModel):
    project_id: int
    problem: str
    scenarios: list[ScenarioResult]  # 항상 3개
