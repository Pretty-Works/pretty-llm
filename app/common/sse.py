"""
SSE 직렬화 — 이벤트 5종을 와이어 형식으로 바꾸는 곳

규격 v2: 진입점 2개(/runs, /resume)의 응답은 모두 text/event-stream 이고,
이벤트는 step · approval_request · question · done · error 다섯 가지다.

와이어 형식 (이벤트 하나):
    event: step\n
    data: {"text":"..."}\n
    \n                       ← 빈 줄이 이벤트의 끝

★ data 는 반드시 한 줄이어야 한다.
  SSE 규약에서 개행은 "다음 필드"를 뜻하므로, JSON 을 들여쓰기로 찍으면
  둘째 줄부터 버려진다. json.dumps 는 기본값이 한 줄이고 문자열 안의
  개행은 \\n 으로 이스케이프되므로 그대로 쓰면 안전하다.
"""

from __future__ import annotations

import json

EVENT_NAMES = ("step", "approval_request", "question", "done", "error")


def sse_event(name: str, data: dict) -> str:
    """이벤트 하나를 SSE 텍스트로 직렬화한다."""
    if name not in EVENT_NAMES:
        raise ValueError(f"규격에 없는 이벤트: {name} ({EVENT_NAMES})")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {payload}\n\n"


def step(text: str) -> str:
    """진행 상황 한 줄. 사용자에게 그대로 노출된다."""
    return sse_event("step", {"text": text})


def error(message: str) -> str:
    """실패 알림. 스트림이 done 없이 끊기면 BE 가 AGENT_017 로 처리하므로,
    예외가 나면 반드시 이걸 마지막으로 내보내야 한다."""
    return sse_event("error", {"message": message})
