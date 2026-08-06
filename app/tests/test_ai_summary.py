# app/tests/test_ai_summary.py
"""프로젝트 탭 AI 요약 생성 — 숫자는 코드가 세고 LLM 은 문장만 쓴다는 계약 검증 (LLM 없이).

재료는 노션 조회 명세의 result 형태를 그대로 흉내 낸 픽스처를 쓰고,
엔드포인트 테스트는 structured_call 만 가짜로 바꿔 배선·응답 형태를 확인한다.
"""

from datetime import date

import httpx

from app.api import project as summary_api
from app.api.project import SummaryRequest
from app.main import app

TODAY = date(2026, 8, 6)


def _materials() -> dict:
    return {
        "projectId": 3,
        "today": "2026-08-06",
        "project": {"name": "그룹웨어 AI 고도화", "startDate": "2026-06-01",
                    "targetDate": "2026-09-30"},
        "milestones": [
            {"milestoneId": 11, "goal": "1차 스프린트 완료", "targetDate": "2026-07-15",
             "completed": True},
            {"milestoneId": 12, "goal": "베타 오픈", "targetDate": "2026-08-20",
             "completed": False, "isNext": True},
        ],
        "tasks": [
            {"taskId": 58, "content": "API 명세 정리", "dueDate": "2026-08-07",
             "completed": False, "assigneeName": "이하늘"},
            {"taskId": 51, "content": "지난주 리뷰 반영", "dueDate": "2026-08-01",
             "completed": False, "isCarryOver": True, "assigneeName": "이하늘"},
            {"taskId": 40, "content": "킥오프 정리", "dueDate": "2026-07-10",
             "completed": True, "assigneeName": "김서준"},
        ],
        "budget": {"targetBudget": 30000000, "spentAmount": 18600000,
                   "remainingAmount": 11400000, "executionRate": 62, "elapsedRate": 54,
                   "byCategory": [{"categoryLabel": "외주비", "share": 65},
                                  {"categoryLabel": "식비", "share": 19}]},
        "expenses": [{"purpose": "IDE 라이선스", "amount": 350000,
                      "spenderName": "이하늘", "expenseDate": "2026-08-01"}],
        "posts": [
            {"title": "부하 테스트 착수 지연 관련 공유", "priority": "HIGH",
             "authorName": "김서준", "department": "PM", "createdAt": "2026-08-04"},
            {"title": "베타 오픈 리스크 점검 요청", "priority": "HIGH",
             "authorName": "이하늘", "department": "백엔드", "createdAt": "2026-08-03"},
            {"title": "베타 오픈 일정 8/20 확정", "priority": "MID",
             "authorName": "김서준", "department": "PM", "createdAt": "2026-08-01"},
            {"title": "이번 주 진행 요약 정리", "priority": "LOW",
             "authorName": "최유나", "department": "프론트", "createdAt": "2026-07-28"},
        ],
        "meetings": [
            {"meetingId": 41, "title": "스프린트 리뷰 3차", "meetingDate": "2026-07-29",
             "authorName": "김서준", "followUp": "API 명세 문서화(이하늘)"},
            {"meetingId": 40, "title": "요구 재정의 킥오프", "meetingDate": "2026-07-20",
             "authorName": "정우진", "followUp": ""},
        ],
        "upcomingMeetings": [{"title": "주간 스크럼", "startAt": "2026-08-06T10:00:00"}],
    }


def _chips(stats):
    return {s.label: s.value for s in stats}


def test_overview_facts_count_and_dday():
    req = SummaryRequest(**_materials())
    facts, stats = summary_api._facts_overview(req, TODAY)
    assert "1개 완료 (50%)" in facts
    assert "마감 임박(7일 내) 1건 / 지연 1건 / 지난주 이월 1건" in facts
    assert "베타 오픈" in facts and "(D-14)" in facts     # 08-20 은 기준일로부터 14일 후
    assert "(D+5)" in facts                               # 08-01 지연 항목
    assert _chips(stats) == {"진행률": "50%", "임박 마감": "1건", "지연": "1건"}


def test_budget_facts_pace_from_given_numbers():
    req = SummaryRequest(**_materials())
    facts, stats = summary_api._facts_budget(req)
    assert "집행률 62%" in facts
    assert "8%p 빠름" in facts                            # 62 - 54 는 코드가 계산
    assert _chips(stats) == {"집행률": "62%", "잔여": "₩11.4M", "외주비": "65%"}


def test_board_facts_priority_counts():
    req = SummaryRequest(**_materials())
    facts, stats = summary_api._facts_board(req)
    assert "전체 4건 / HIGH 2건 / MID 1건" in facts
    assert "부하 테스트 착수 지연" in facts
    assert _chips(stats) == {"전체": "4건", "HIGH": "2건", "MID": "1건"}


def test_meeting_facts_followup_and_next():
    req = SummaryRequest(**_materials())
    facts, stats = summary_api._facts_meeting(req, TODAY)
    assert "기록 2건, 후속 액션 미정리 1건" in facts
    assert _chips(stats) == {"회의": "2건", "후속 액션": "미정리 1건", "다음 회의": "등록됨"}


async def test_endpoint_returns_four_sections(monkeypatch):
    async def fake_structured_call(messages, schema, **kwargs):
        assert "사실 묶음" in messages[-1]["content"]
        section = lambda text: {"headline": text, "detail": [text + " 상세예요"]}
        return schema(overview=section("개요"), board=section("게시판"),
                      budget=section("재무"), meeting=section("회의록"))

    monkeypatch.setattr(summary_api.llm_client, "structured_call", fake_structured_call)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/agent/project-summary", json=_materials())

    assert res.status_code == 200
    body = res.json()
    assert body["projectId"] == 3                          # BE→LLM 규격: 봉투 없음
    assert [s["section"] for s in body["summaries"]] == ["overview", "board",
                                                         "budget", "meeting"]
    finance = body["summaries"][2]
    assert finance["headline"] == "재무"
    assert finance["detail"] == ["재무 상세예요"]
    assert {"label": "집행률", "value": "62%"} in finance["stats"]   # 칩은 코드가 계산


async def test_endpoint_rejects_bad_today():
    bad = _materials() | {"today": "2026/08/06"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/agent/project-summary", json=bad)
    assert res.status_code == 422
