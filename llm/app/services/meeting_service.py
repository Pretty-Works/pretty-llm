import json

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.schemas.response import (
    FollowupItem,
    MeetingFollowupResult,
    MeetingSummaryResult,
)

client = AsyncOpenAI(api_key=settings.openai_api_key)


async def fetch_meeting(meeting_id: int, http: httpx.AsyncClient) -> dict:
    """내부 API에서 회의록 데이터 조회"""
    response = await http.get(f"/internal/v1/meetings/{meeting_id}")
    response.raise_for_status()
    return response.json()["result"]


async def summarize_meeting(meeting_id: int, http: httpx.AsyncClient) -> MeetingSummaryResult:
    meeting = await fetch_meeting(meeting_id, http)
    content = meeting.get("content", "")

    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 회의록을 간결하게 요약하는 어시스턴트입니다. "
                    "핵심 논의 사항과 결정된 내용을 중심으로 3~5문장으로 요약하세요."
                ),
            },
            {
                "role": "user",
                "content": f"다음 회의록을 요약해주세요:\n\n{content}",
            },
        ],
    )

    summary = response.choices[0].message.content.strip()
    return MeetingSummaryResult(meeting_id=meeting_id, summary=summary)


async def extract_followup(meeting_id: int, http: httpx.AsyncClient) -> MeetingFollowupResult:
    meeting = await fetch_meeting(meeting_id, http)

    # 이미 후속조치가 있으면 LLM 호출 없이 바로 반환
    existing = meeting.get("follow_up")
    if existing:
        if isinstance(existing, list):
            items = [FollowupItem(action=a) for a in existing]
        else:
            items = [FollowupItem(action=str(existing))]
        return MeetingFollowupResult(meeting_id=meeting_id, follow_ups=items)

    content = meeting.get("content", "")

    response = await client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 회의록에서 후속조치(Action Item)를 추출하는 어시스턴트입니다. "
                    "각 항목을 JSON으로 반환하세요. "
                    "형식: {\"follow_ups\": [{\"action\": \"...\", \"assignee\": \"...\", \"due_date\": \"...\"}]} "
                    "담당자나 기한이 명시되지 않은 경우 null로 채우세요."
                ),
            },
            {
                "role": "user",
                "content": f"다음 회의록에서 후속조치를 추출해주세요:\n\n{content}",
            },
        ],
        response_format={"type": "json_object"},
    )

    raw = json.loads(response.choices[0].message.content)
    items = [FollowupItem(**item) for item in raw.get("follow_ups", [])]
    return MeetingFollowupResult(meeting_id=meeting_id, follow_ups=items)
