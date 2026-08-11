# app/tests/test_suggestions.py
"""추천 문구 API — 후보 선정은 코드가 한다는 계약을 검증한다.

LLM 이 무엇을 추천할지 정하면 "없는 회의"를 권하는 걸 막을 수 없다. 그래서
후보는 재료에서 코드가 고르고, LLM 은 문장만 바꾼다. 여기서는 그 선정 로직과
LLM 이 죽었을 때의 폴백을 본다 (LLM 호출 없음).
"""

from datetime import date

import pytest

from app.api import suggestions as mod
from app.api.suggestions import MAX_SUGGESTIONS, SuggestionRequest, _candidates

TODAY = date(2026, 8, 11)

FULL = {
    "today": "2026-08-11",
    "projects": [{"projectId": 3, "name": "그룹웨어 AI 고도화", "targetDate": "2026-09-30"}],
    "tasks": [
        {"taskId": 1, "content": "환율 연동 검증", "dueDate": "2026-08-06", "completed": False},
        {"taskId": 2, "content": "API 명세 정리", "dueDate": "2026-08-13", "completed": False},
        {"taskId": 3, "content": "끝난 일", "dueDate": "2026-08-01", "completed": True},
    ],
    "meetings": [{"meetingId": 41, "title": "스프린트 리뷰 3차",
                  "meetingDate": "2026-07-29", "followUp": ""}],
    "upcomingMeetings": [{"title": "주간 스크럼", "startAt": "2026-08-12T10:00:00"}],
    "leaveBalance": {"remainingDays": 12},
}


def _req(**over) -> SuggestionRequest:
    return SuggestionRequest(**{**FULL, **over})


def test_지연된_할일이_가장_먼저_추천된다() -> None:
    picked = _candidates(_req(), TODAY)
    assert picked[0]["kind"] == "overdue_task"
    assert "1건" in picked[0]["text"]          # 완료된 일은 세지 않는다


def test_추천은_최대_3개다() -> None:
    assert len(_candidates(_req(), TODAY)) == MAX_SUGGESTIONS


def test_후속액션_비어있는_회의를_집어낸다() -> None:
    kinds = [c["kind"] for c in _candidates(_req(), TODAY)]
    assert "meeting_followup" in kinds


def test_후속액션이_정리된_회의는_추천하지_않는다() -> None:
    meetings = [{**FULL["meetings"][0], "followUp": "API 명세 문서화(이하늘)"}]
    kinds = [c["kind"] for c in _candidates(_req(meetings=meetings), TODAY)]
    assert "meeting_followup" not in kinds


def test_재료가_비면_추천도_없다() -> None:
    empty = SuggestionRequest(today="2026-08-11")
    assert _candidates(empty, TODAY) == []


def test_최근에_물어본_것은_다시_추천하지_않는다() -> None:
    picked = _candidates(_req(recentQuestions=["스프린트 리뷰 3차 후속 액션 정리해줘"]), TODAY)
    assert all(c["kind"] != "meeting_followup" for c in picked)


def test_추천마다_실행가능한_요청문이_붙는다() -> None:
    for c in _candidates(_req(), TODAY):
        assert c["prompt"] and len(c["prompt"]) > 5
        assert "그거" not in c["prompt"] and "아까" not in c["prompt"]


@pytest.mark.asyncio
async def test_LLM이_죽어도_코드_문구로_폴백한다(monkeypatch) -> None:
    async def _boom(*a, **k):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(mod.llm_client, "structured_call", _boom)
    res = await mod.suggestions(_req())
    assert len(res.suggestions) == MAX_SUGGESTIONS
    assert res.suggestions[0].text.endswith("?")      # 코드 폴백 문구가 그대로 나온다
    assert res.suggestions[0].kind == "overdue_task"


@pytest.mark.asyncio
async def test_today_형식이_틀리면_422() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        await mod.suggestions(_req(today="2026/08/11"))
    assert e.value.status_code == 422
