# app/tests/test_meeting_draft.py
"""회의록 초안 생성 — 명단 검증·길이 절단·날짜 검증은 코드 몫이라는 계약 (LLM 없이)."""

import httpx

from app.api import meeting as draft_api
from app.main import app


def _payload():
    return {
        "transcript": "김서준: 오늘 스프린트 리뷰 시작하겠습니다. 백엔드 API 68% 완료입니다.",
        "today": "2026-08-07",
        "projectMembers": [
            {"userId": 1, "name": "김서준", "department": "개발팀", "position": "팀장"},
            {"userId": 2, "name": "이하늘", "department": "백엔드팀", "position": "대리"},
        ],
    }


async def test_endpoint_sanitizes_llm_output(monkeypatch):
    async def fake_structured_call(messages, schema, **kwargs):
        user_msg = messages[-1]["content"]
        assert "회의 기록 전문" in user_msg
        assert "userId=1" in user_msg                    # 참여자 명단이 프롬프트에 들어간다
        return schema(title="스" * 300, meetingDate="26-08-01",
                      content="· 백엔드 API 68% 완료",
                      attendeeUserIds=[1, 2, 99, 1])     # 명단 밖 99 + 중복 1

    monkeypatch.setattr(draft_api.llm_client, "structured_call", fake_structured_call)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/agent/meeting-draft", json=_payload())

    assert res.status_code == 200
    body = res.json()                                    # BE→LLM 규격: 봉투 없음
    assert len(body["title"]) == 200                     # 200자 절단
    assert body["meetingDate"] is None                   # 형식 불량 날짜 → null
    assert body["attendeeUserIds"] == [1, 2]             # 명단 밖 제거 + 중복 제거
    assert body["content"] == "· 백엔드 API 68% 완료"
    assert body["location"] is None and body["followUp"] is None   # 근거 없으면 null 유지


async def test_endpoint_rejects_bad_today():
    bad = _payload() | {"today": "2026/08/07"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/agent/meeting-draft", json=bad)
    assert res.status_code == 422
