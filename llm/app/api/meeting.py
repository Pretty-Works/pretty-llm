import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_internal_client
from app.schemas.response import MeetingExtractFollowupResponse, MeetingSummarizeResponse
from app.services import meeting_service

router = APIRouter()


@router.post("/{meeting_id}/summarize", response_model=MeetingSummarizeResponse)
async def summarize_meeting(
    meeting_id: int,
    http: httpx.AsyncClient = Depends(get_internal_client),
):
    try:
        result = await meeting_service.summarize_meeting(meeting_id, http)
        return MeetingSummarizeResponse(result=result)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="회의록 조회 실패")


@router.post("/{meeting_id}/extract-followup", response_model=MeetingExtractFollowupResponse)
async def extract_followup(
    meeting_id: int,
    http: httpx.AsyncClient = Depends(get_internal_client),
):
    try:
        result = await meeting_service.extract_followup(meeting_id, http)
        return MeetingExtractFollowupResponse(result=result)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail="회의록 조회 실패")
