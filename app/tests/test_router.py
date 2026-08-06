"""
오케스트레이터 3분기 테스트

시나리오:
  ① classify 단독 — 명확한 문장 3개가 각자의 길로 가는가
  ② simple_query 관통 — 조회는 승인 없이 done 까지 (HITL 이벤트가 없어야 함)
  ③ engine_b 라우팅 — 스텁이지만 계약(step→done)대로 응답하는가
  ④ route 영속 — engine_a 로 멈춘 run 의 체크포인트 metadata 에 route 가
     저장되어, resume 이 재분류 없이 같은 에이전트로 돌아가는가

실행:  uv run python -m app.tests.test_router
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from app.main import app


def _body(goal: str) -> dict:
    return {
        "runId": f"run_{uuid.uuid4().hex[:8]}",
        "conversationId": 12,
        "goal": goal,
        "messages": [],
        "screenContext": {"screen": "HOME", "formState": {}},
        "requestSource": "WEB",
        "locale": "ko-KR",
    }


async def _collect_sse(client, url, body):
    events, name = [], None
    async with client.stream("POST", url, json=body) as res:
        assert res.status_code == 200, f"{url} → {res.status_code}"
        async for line in res.aiter_lines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[len("data: "):])))
    return events


async def main() -> None:
    try:
        await _scenarios()
    finally:
        from app.common.checkpoint import close_checkpointer
        await close_checkpointer()


async def _scenarios() -> None:
    # ── ① classify 단독 ──────────────────────────────────────
    from app.orchestrator.classify import classify

    cases = {
        "내 연차 며칠 남았어?": "simple_query",
        "그룹웨어 프로젝트에 오늘 스프린트 리뷰 회의록 올려줘": "engine_a",
        "일정이 밀렸는데 어떻게 조정할지 시나리오 분석해줘": "engine_b",
    }
    for goal, expected in cases.items():
        got = await classify(goal)
        assert got.route == expected, f"{goal!r} → {got.route} (기대: {expected})"
    print("✅ ① classify: 조회/실행/분석 3방향 정확", flush=True)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=90) as client:

        # ── ② simple_query — 승인 없이 done ──────────────────
        events = await _collect_sse(client, "/api/agent/runs",
                                    _body("그룹웨어 프로젝트 회의록 목록 보여줘"))
        names = [n for n, _ in events]
        assert names[-1] == "done", f"done 으로 안 끝남: {names}"
        assert "approval_request" not in names and "question" not in names, \
            f"조회에 HITL 이벤트가 섞임: {names}"
        answer = events[-1][1]["answer"]
        assert "41" in answer or "회의" in answer, answer
        print(f"✅ ② simple_query: {names} → 승인 없이 done ({answer[:40]!r})", flush=True)

        # ── ③ engine_b 라우팅 (실물 분석 엔진) ───────────────
        events = await _collect_sse(client, "/api/agent/runs",
                                    _body("이 프로젝트 일정 리스크를 시나리오별로 분석해줘"))
        names = [n for n, _ in events]
        assert names[-1] == "done" and names.count("step") >= 1, f"계약 위반: {names}"
        assert len(events[-1][1]["answer"]) > 20, events[-1][1]
        print(f"✅ ③ engine_b 라우팅: 분류 → 분석 엔진 완주 ({names})", flush=True)

        # ── ④ route 영속 — resume 이 같은 에이전트로 ─────────
        body = _body("그룹웨어 프로젝트에 오늘 '주간 점검' 회의록 올려줘. "
                     "참석자 김서준·이하늘, 목적 진행 공유, 내용 API 68% 완료.")
        events = await _collect_sse(client, "/api/agent/runs", body)
        last = events[-1][0]
        assert last in ("approval_request", "question"), f"멈춤 없이 끝남: {last}"

        from app.common.checkpoint import get_checkpointer
        saver = await get_checkpointer()
        tup = await saver.aget_tuple({"configurable": {"thread_id": body["runId"]}})
        assert tup is not None, "체크포인트가 없음"
        stored = (tup.metadata or {}).get("route")
        assert stored == "engine_a", f"metadata 에 route 가 없음: {tup.metadata}"
        print("✅ ④ route 영속: 체크포인트 metadata 에 engine_a 저장 → resume 복원 가능", flush=True)

    print("\n3분기 관통 성공", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
