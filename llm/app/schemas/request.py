from pydantic import BaseModel


class MeetingSummarizeRequest(BaseModel):
    """POST /meetings/{id}/summarize"""
    pass  # meeting_id는 path parameter로 받으므로 body 없음


class MeetingExtractFollowupRequest(BaseModel):
    """POST /meetings/{id}/extract-followup"""
    pass  # meeting_id는 path parameter로 받으므로 body 없음
