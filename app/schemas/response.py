from typing import Any, Optional
from pydantic import BaseModel


# 공통 응답 포맷
class BaseResponse(BaseModel):
    errorCode: Optional[str] = None
    message: str = "success"
    result: Optional[Any] = None


# 회의록 요약
class MeetingSummaryResult(BaseModel):
    meeting_id: int
    summary: str                    # 전체 요약문


class MeetingSummarizeResponse(BaseResponse):
    result: Optional[MeetingSummaryResult] = None


# 후속조치 추출
class FollowupItem(BaseModel):
    action: str                     # 해야 할 일
    assignee: Optional[str] = None  # 담당자 (추출 가능한 경우)
    due_date: Optional[str] = None  # 기한 (추출 가능한 경우)


class MeetingFollowupResult(BaseModel):
    meeting_id: int
    follow_ups: list[FollowupItem]


class MeetingExtractFollowupResponse(BaseResponse):
    result: Optional[MeetingFollowupResult] = None
