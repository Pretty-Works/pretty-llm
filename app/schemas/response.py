"""
응답 형식  —  서버 → 프론트로 나가는 body

모든 응답은 공통 래퍼 { errorCode, message, result } 로 감싼다.
프론트 개발자는 이 파일만 보면 "뭘 받는지" 알 수 있어야 한다.

담당자 1이 공통 래퍼 + HITL 제안 응답, 각 도메인 상세는 담당자가 채운다.
Pydantic v2.
"""

from __future__ import annotations

from typing import Any, Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, Field

from app.schemas.state import Domain

T = TypeVar("T")


# ─── 공통 래퍼 (모든 응답이 이 형태)  ───────────────────────
# FastAPI가 Spring 내부 API로 데이터 조회할 때 Spring이 돌려주는 것
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


# ─── FastAPI → BE SSE 이벤트 5종 (연동 규격 v2 §4) ──────────
# event: step
class StepEvent(BaseModel):
    text: str                                      # 진행 상황 한 줄. 100자 이하

# event: approval_request
class ApprovalOption(BaseModel):
    id: str
    label: str

class ApprovalRequestEvent(BaseModel):
    toolCallId: str                                # LLM팀이 발급하는 도구 호출 식별자
    tool: str                                      # 예: meeting.create
    access: Literal["READ", "WRITE"]               # BE가 해석하는 유일한 필드
    summary: str                                   # 승인 카드 제목. 60자 이하
    params: dict[str, Any]                         # 내부 API 요청 바디 원본
    alternatives: list[ApprovalOption] = Field(default_factory=list)
    # previewText는 BE가 params에서 생성 → LLM이 보내지 않음

# event: question
class QuestionOption(BaseModel):
    id: str
    label: str
    description: Optional[str] = None

class QuestionEvent(BaseModel):
    text: str                                      # 질문 문구. 200자 이하
    options: list[QuestionOption] = Field(default_factory=list)
    multiple: bool = False
    allowFreeText: bool = True

# event: done
class ActionPayload(BaseModel):
    type: str                                      # NAVIGATE | FILL_FORM
    label: str                                     # 버튼 문구. 30자 이하
    targetScreen: str                              # 화면 식별자
    params: dict[str, Any] = Field(default_factory=dict)
    formData: dict[str, Any] = Field(default_factory=dict)  # FILL_FORM일 때만

class DoneEvent(BaseModel):
    answer: str                                    # 말풍선 텍스트
    action: Optional[ActionPayload] = None         # 없으면 null

# event: error
class ErrorEvent(BaseModel):
    code: str                                      # AGENT_0XX
    message: str                                   # 사용자에게 보일 실패 사유


class AgentResponse(BaseModel):
    answer: str                                    # 말풍선 텍스트 (v1 호환용)
    success: bool                                  # LLM팀이 직접 판정
    action: Optional[ActionPayload] = None         # 없으면 null
    raw: Optional[dict[str, Any]] = None           # 디버깅용 원본


# ─── 회의록 (담당자3) ────────────────────────────────────────
class MeetingSummaryResult(BaseModel):
    meeting_id: int
    summary: str


class FollowupItem(BaseModel):
    action: str
    assignee: Optional[str] = None
    due_date: Optional[str] = None


class MeetingFollowupResult(BaseModel):
    meeting_id: int
    follow_ups: list[FollowupItem]


MeetingSummarizeResponse = ApiResponse[MeetingSummaryResult]
MeetingExtractFollowupResponse = ApiResponse[MeetingFollowupResult]

# TODO (각 도메인 담당자):
#   - 프로젝트/마일스톤/Task/지출/게시글/회의록 조회·생성 응답 result 모델
#   - 대부분 ApiResponse[구체타입] 형태로 감싸 반환