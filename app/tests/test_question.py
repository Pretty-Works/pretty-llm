"""
question(되묻기) 관통 테스트

시나리오:
  ① 정보가 빠진 goal("회의록 올려줘") → 세그먼트가 question 으로 끝남
  ② question 대기 중에 decision 을 보내면 → 400 (재개 방식 불일치)
  ③ answer 로 재개 → (추가 질문 최대 3번 허용) → approval_request 도달
  ④ 5회 한도 — 이미 5번 물었으면 interrupt 없이 거절 문구 반환 (단위)

실행:  uv run python -m app.tests.test_question
"""

from __future__ import annotations

import asyncio
import json
import types
import uuid

import httpx

from app.main import app


async def _collect_sse(client, method, url, body):
    events, name = [], None
    async with client.stream(method, url, json=body) as res:
        assert res.status_code == 200, f"{url} → {res.status_code}"
        async for line in res.aiter_lines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[len("data: "):])))
    return events


ANSWER = ("그룹웨어 프로젝트야. 제목은 '주간 점검', 날짜는 2026-08-04, "
          "참석자는 김서준·이하늘, 목적은 진행 상황 공유, 내용은 백엔드 API 68% 완료.")


async def main() -> None:
    try:
        await _scenarios()
    finally:
        from app.common.checkpoint import close_checkpointer
        await close_checkpointer()


async def _scenarios() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=90) as client:

        # ── ① 모호한 goal → question ─────────────────────────
        # 모델이 ask_user 대신 텍스트(done)로 물을 때도 있다 — 그건 A-2 로
        # 합법이지만, 이 테스트의 목적은 question "이벤트 기계" 검증이므로
        # question 이 나올 때까지 새 Run 을 최대 3번 시도한다.
        for attempt in range(3):
            run_id = f"run_{uuid.uuid4().hex[:8]}"
            events = await _collect_sse(client, "POST", "/api/agent/runs", {
                "runId": run_id, "conversationId": 12, "goal": "회의록 올려줘",
                "messages": [], "screenContext": {"screen": "HOME", "formState": {}},
                "requestSource": "WEB", "locale": "ko-KR",
            })
            names = [n for n, _ in events]
            if names[-1] == "question":
                break
        assert names[-1] == "question", f"3회 모두 question 없이 종료: {names}"
        q = events[-1][1]
        for field in ("questionId", "label", "text", "options", "multiple", "allowFreeText"):
            assert field in q, f"question 에 {field} 없음: {q}"
        print(f"✅ ① 모호한 goal → question: {q['label']!r} / {q['text'][:40]!r}", flush=True)

        # ── ② question 대기에 decision 보내면 400 ────────────
        res = await client.post(f"/api/agent/runs/{run_id}/resume",
                                json={"toolCallId": "tc_x", "decision": "APPROVED"})
        assert res.status_code == 400, res.status_code
        assert "answer" in res.json()["detail"]["message"]
        print("✅ ② question 대기에 decision → 400 (재개 방식 검증)", flush=True)

        # ── ③ answer 재개 → (추가 질문 허용) → approval 도달 ──
        last = "question"
        for hop in range(4):
            events = await _collect_sse(client, "POST",
                                        f"/api/agent/runs/{run_id}/resume",
                                        {"answer": ANSWER})
            last = events[-1][0]
            if last != "question":
                break
        assert last == "approval_request", f"승인까지 못 감 (마지막: {last})"
        approval = events[-1][1]
        assert approval["tool"] == "meeting.create", approval
        print(f"✅ ③ answer 재개 → {hop + 1}세그먼트 만에 approval_request 도달", flush=True)

    # ── ④ 5회 한도 (단위) ────────────────────────────────────
    from app.tools.ask_user import ask_user

    asked5 = [types.SimpleNamespace(type="tool", name="ask_user") for _ in range(5)]
    rt = types.SimpleNamespace(state={"messages": asked5}, context=None)
    out = ask_user.func(label="추가 질문", text="더 물어봐도 될까요?", runtime=rt)
    assert "한도" in out, out
    print("✅ ④ 5회 한도 → interrupt 없이 거절 문구 (AGENT_022 가드)", flush=True)

    print("\nquestion 관통 성공", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
