# app/tests/test_meeting_draft.py
"""회의록 초안 생성 — 명단 검증·길이 절단·날짜 검증은 코드 몫이라는 계약 (LLM 없이).

경로 A(화면 txt 업로드 → meeting-draft API)와 경로 B(채팅 첨부 → FILL_FORM 도구)
둘 다 같은 초안 코어(generate_draft)를 쓴다.
"""

from types import SimpleNamespace

import httpx

from app.api import meeting as draft_api
from app.main import app
from app.tools.meeting_tool import meeting_draft_fill
from app.tools.registry import RunContext


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


async def test_fill_tool_sets_fill_form_action(monkeypatch):
    async def fake_structured_call(messages, schema, **kwargs):
        return schema(title="스프린트 리뷰", meetingDate="2026-08-05",
                      content="· 진행 점검", attendeeUserIds=[5, 99])   # 99 는 명단 밖

    monkeypatch.setattr(draft_api.llm_client, "structured_call", fake_structured_call)

    ctx = RunContext(run_id="t-run",
                     attachments=[{"name": "회의.txt", "content": "김서준: 리뷰 시작합니다"}])
    out = await meeting_draft_fill.coroutine(projectId=3,
                                             runtime=SimpleNamespace(context=ctx))

    assert "초안" in out
    action = ctx.action                                  # 저장 없이 폼 채움 액션만 남는다
    assert action["type"] == "FILL_FORM"
    assert action["targetScreen"] == "MEETING_CREATE"
    assert action["label"]
    assert action["formData"]["title"] == "스프린트 리뷰"
    assert action["formData"]["attendeeIds"] == [5]      # mock 명단(2·5·7) 밖 99 제거
    assert action["formData"]["recording"] is None


async def test_fill_tool_without_attachment():
    ctx = RunContext(run_id="t-run")
    out = await meeting_draft_fill.coroutine(projectId=3,
                                             runtime=SimpleNamespace(context=ctx))
    assert "첨부된 파일이 없" in out
    assert ctx.action is None                            # 액션도 만들지 않는다
