# app/tests/test_engine_b_history_wiring.py
"""엔진B의 "순수 분석" 경로(mode=analysis, replan 아님)가 simple_query/engine_a/
replan과 달리 여태 대화 기록(history)을 아예 못 받던 문제를 고친 걸 확인한다.

★ 배경
  api/agent.py의 start_run()은 어느 라우트로 가든 history(RunRequest.messages)를
  갖고 있지만, engine_b의 mode=analysis 분기(_engine_b_stream)만 그걸 안 넘기고
  goal 텍스트 하나만 run_engine_b()에 넘겼다. "아까 그 분석 이어서 X도 봐줘" 같은
  요청이 이 경로에서는 매번 백지 상태로 시작됐다는 뜻이다.
  AnalysisRequest.chat_history 필드와 analysis_router._render_user()의
  "[직전 대화]" 렌더링은 원래부터 있었다 — runner.py가 그 필드를 안 채워서
  안 쓰이고 있었을 뿐이다.
"""

from __future__ import annotations

from app.engine_b.runner import _format_history


def test_format_history_empty_returns_empty_string():
    assert _format_history(None) == ""
    assert _format_history([]) == ""


def test_format_history_renders_recent_turns():
    history = [
        {"role": "USER", "content": "이 프로젝트 우선순위 분석해줘"},
        {"role": "AGENT", "content": "1순위는 API 명세, 2순위는 인증 모듈입니다."},
    ]
    rendered = _format_history(history)
    assert "이전 대화:" in rendered
    assert "USER: 이 프로젝트 우선순위 분석해줘" in rendered
    assert "AGENT: 1순위는 API 명세, 2순위는 인증 모듈입니다." in rendered


def test_format_history_keeps_only_last_four_turns():
    history = [{"role": "USER", "content": f"turn{i}"} for i in range(10)]
    rendered = _format_history(history)
    assert "turn9" in rendered
    assert "turn6" in rendered   # 마지막 4개(6,7,8,9) 중 하나
    assert "turn5" not in rendered  # 그 앞은 잘림
    assert "turn0" not in rendered  # 오래된 건 잘림


async def test_engine_b_stream_passes_history_through_to_run_engine_b(monkeypatch):
    """api/agent.py._engine_b_stream()이 받은 history를 runner.run_engine_b()의
    history 인자로 그대로 넘기는지 — 시그니처만 바뀌고 실제로 안 흘려보내면
    아무 의미 없으므로 이 연결 자체를 확인한다."""
    import app.api.agent as agent_module

    captured: dict = {}

    async def fake_run_engine_b(goal, run_id, screen="HOME", history=None):
        captured["goal"] = goal
        captured["run_id"] = run_id
        captured["history"] = history
        yield {"type": "result", "answer": "ok"}

    monkeypatch.setattr("app.engine_b.runner.run_engine_b", fake_run_engine_b)

    sent_history = [{"role": "USER", "content": "이전 질문"}]
    events = [ev async for ev in agent_module._engine_b_stream(
        "이어서 분석해줘", "run_123", sent_history)]

    assert captured["history"] == sent_history
    assert captured["goal"] == "이어서 분석해줘"
    assert captured["run_id"] == "run_123"
    assert any("done" in ev for ev in events)  # done 이벤트까지 정상 완주(예외 없음)
