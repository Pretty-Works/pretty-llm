# app/tests/test_ask_user_options.py
"""ask_user 가 내보내는 question 페이로드 계약 검증.

보기(options)는 사용자가 눌러서 고르는 값이라, 우리가 넣은 이름표가 그대로
전달돼야 한다. FE 는 보기를 버튼으로 그리고 누르는 즉시 그 하나를 제출한다
(2026-08-10 번들 확인) — 그래서 multiple 을 켜도 실제로는 단일 선택이다.
"""

import pytest

from app.tools import ask_user as mod
from app.tools.ask_user import QUESTION_LIMIT, ask_user


class _Runtime:
    """ToolRuntime 대역 — ask_user 는 state.messages 만 본다."""

    def __init__(self, asked: int = 0) -> None:
        class _Msg:
            type = "tool"
            name = "ask_user"

        self.state = {"messages": [_Msg() for _ in range(asked)]}
        self.context = None


@pytest.fixture
def asked_payload(monkeypatch):
    """interrupt() 는 그래프 안에서만 부를 수 있어 가로채고 payload 를 남긴다."""
    box = {}

    def _fake(payload):
        box["payload"] = payload
        return "사용자가 고른 값"

    monkeypatch.setattr(mod, "interrupt", _fake)
    return box


def _call(**kwargs):
    args = {"label": "프로젝트 선택", "text": "어느 프로젝트인가요?", "runtime": _Runtime()}
    args.update(kwargs)
    return ask_user.func(**args)


def test_보기_이름표가_그대로_전달된다(asked_payload) -> None:
    out = _call(options=["다온증권 해외주식 주문 개선", "그룹웨어 AI 고도화"])
    payload = asked_payload["payload"]
    assert payload["kind"] == "question"
    assert [o["label"] for o in payload["options"]] == [
        "다온증권 해외주식 주문 개선", "그룹웨어 AI 고도화"]
    assert "사용자가 고른 값" in out


def test_보기가_없어도_자유입력으로_물을_수_있다(asked_payload) -> None:
    _call(label="회의 제목", text="회의 제목을 뭐라고 할까요?")
    payload = asked_payload["payload"]
    assert payload["options"] == []
    assert payload["allowFreeText"] is True   # FE 가 이 값으로 "직접 입력" 칸을 그린다


def test_질문_한도를_넘으면_묻지_않고_거절한다() -> None:
    out = ask_user.func(label="x", text="y", options=["a"],
                        runtime=_Runtime(asked=QUESTION_LIMIT))
    assert "한도" in out


# ★ 8/12 추가 — option_details: 보기(label) 하나만으로는 차이를 알 수 없는 경우
#   (재계획 3안 등) 보기별 부가 설명을 실어 보낼 수 있어야 한다.
def test_option_details가_같은_순서로_설명에_실린다(asked_payload) -> None:
    _call(options=["일정 조정", "범위 축소"],
          option_details=["마감을 2주 미룬다. risk=낮음", "비핵심 태스크를 제외한다. risk=중간"])
    payload = asked_payload["payload"]
    assert [o["description"] for o in payload["options"]] == [
        "마감을 2주 미룬다. risk=낮음", "비핵심 태스크를 제외한다. risk=중간"]


def test_option_details가_없으면_description은_None이다(asked_payload) -> None:
    _call(options=["일정 조정", "범위 축소"])
    payload = asked_payload["payload"]
    assert [o["description"] for o in payload["options"]] == [None, None]


def test_option_details가_options보다_짧아도_에러없이_남는_항목은_None(asked_payload) -> None:
    _call(options=["일정 조정", "범위 축소", "인력 재배치"],
          option_details=["마감을 2주 미룬다."])
    payload = asked_payload["payload"]
    assert [o["description"] for o in payload["options"]] == [
        "마감을 2주 미룬다.", None, None]
