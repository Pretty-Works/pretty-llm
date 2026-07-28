"""
응답 형식  —  서버 → 프론트로 나가는 body

모든 응답은 공통 래퍼 { errorCode, message, result } 로 감싼다.
프론트 개발자는 이 파일만 보면 "뭘 받는지" 알 수 있어야 한다.

담당자 1이 공통 래퍼 + HITL 제안 응답, 각 도메인 상세는 담당자가 채운다.
Pydantic v2.
"""

from __future__ import annotations

from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

from app.schemas.state import Domain

T = TypeVar("T")


# ─── 공통 래퍼 (모든 응답이 이 형태) ───────────────────────
class ApiResponse(BaseModel, Generic[T]):
    errorCode: Optional[str] = None      # 성공 시 None, 실패 시 "PROJECT_001" 등(가안)
    message: str = "SUCCESS"
    result: Optional[T] = None

    @classmethod
    def ok(cls, result: T | None = None) -> "ApiResponse[T]":
        return cls(errorCode=None, message="SUCCESS", result=result)

    @classmethod
    def fail(cls, code: str, message: str) -> "ApiResponse[T]":
        return cls(errorCode=code, message=message, result=None)


# ─── AI 요약 (탭 공통, 읽기 전용 → HITL 없음) ──────────────
class StatItem(BaseModel):
    label: str
    value: str


class AiSummaryResult(BaseModel):
    scope: str                           # progress | budget | board | meeting
    headline: str
    detail: list[str]
    stats: list[StatItem]
    generatedAt: str


# ─── HITL 제안 응답 (담당자 1) ─────────────────────────────
#     생성형 기능이 반환하는 "제안". 아직 DB 미반영, suggestionId로 확정 API 호출.
class SuggestionResponse(BaseModel):
    suggestionId: str
    domain: Domain
    kind: str                            # "staffing" | "replan" | "action_items" ...
    payload: dict[str, Any]              # 후보/시나리오 등 제안 본문
    tradeoffs: list[str] = Field(default_factory=list)
    generatedAt: str


# ─── HITL 확정 결과 (담당자 1) ─────────────────────────────
class DecisionResult(BaseModel):
    action: str                          # approve | reject | replan
    applied: dict[str, Any] = Field(default_factory=dict)   # approve 시 실제 반영 내역
    suggestionId: Optional[str] = None   # replan 시 새로 발급된 제안 ID
    attempt: Optional[int] = None        # replan 재시도 횟수
    auditLogId: Optional[str] = None


# TODO (각 도메인 담당자):
#   - 프로젝트/마일스톤/Task/지출/게시글/회의록 조회·생성 응답 result 모델
#   - 대부분 ApiResponse[구체타입] 형태로 감싸 반환
