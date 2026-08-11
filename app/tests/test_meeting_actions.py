# app/tests/test_meeting_actions.py
"""회의록 실행 항목 추출 — 코드가 지키는 안전선을 검증한다 (LLM 호출 없음).

핵심 계약 두 가지:
  ① 상태(진행중·완료)를 응답에 넣지 않는다. 지금부터 할 일이라 의미가 없다.
  ② 담당자는 참여자 명단에 있는 사람만. 명단 밖 이름은 null 로 떨군다 —
     틀린 사람을 지정하면 엉뚱한 사람에게 할 일이 생긴다.
"""

import pytest

from app.api.meeting import (ActionItem, ActionRequest, Member, _ActionDraft,
                             _sanitize_actions, meeting_actions)

MEMBERS = [
    Member(userId=5, name="김하늘", department="백엔드개발", position="사원"),
    Member(userId=7, name="박도윤", department="인프라", position="대리"),
]


def _draft(**over) -> _ActionDraft:
    return _ActionDraft(**{"action": "결제 모듈 연동 범위 확정",
                           "assignee": "김하늘", "dueDate": "2026-08-20", **over})


def test_명단에_있는_담당자는_id_로_매칭된다() -> None:
    out = _sanitize_actions([_draft()], MEMBERS)
    assert out[0].assigneeId == 5 and out[0].assigneeName == "김하늘"


def test_명단_밖_담당자는_비운다() -> None:
    out = _sanitize_actions([_draft(assignee="정민재")], MEMBERS)
    assert out[0].assigneeId is None and out[0].assigneeName is None
    assert out[0].action                      # 항목 자체는 살린다


def test_마감일_형식이_틀리면_비운다() -> None:
    out = _sanitize_actions([_draft(dueDate="다음 주 금요일")], MEMBERS)
    assert out[0].dueDate is None


def test_마감일이_없어도_항목은_남는다() -> None:
    out = _sanitize_actions([_draft(dueDate=None)], MEMBERS)
    assert out[0].dueDate is None and out[0].action


def test_실행항목에_상태_필드가_없다() -> None:
    """상태를 되살리면 이 테스트가 깨진다 — 의도적으로 뺀 필드다."""
    fields = set(ActionItem.model_fields)
    assert fields == {"action", "assigneeId", "assigneeName", "dueDate"}
    assert "status" not in fields and "state" not in fields


def test_빈_항목은_버린다() -> None:
    assert _sanitize_actions([_draft(action="   ")], MEMBERS) == []


def test_열_개를_넘으면_자른다() -> None:
    out = _sanitize_actions([_draft() for _ in range(15)], MEMBERS)
    assert len(out) == 10


@pytest.mark.asyncio
async def test_회의_내용이_비면_LLM_을_부르지_않는다() -> None:
    res = await meeting_actions(ActionRequest(today="2026-08-11", content="", followUp=""))
    assert res.actions == []


@pytest.mark.asyncio
async def test_today_형식이_틀리면_422() -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        await meeting_actions(ActionRequest(today="2026/08/11", content="논의함"))
    assert e.value.status_code == 422
