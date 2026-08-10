"""
엔진 B 결합 관통 — 직행(오케스트레이터) · 측면(엔진 A 도구) · 진행 중계

시나리오:
  ① run_engine_b 단독 — 계약(progress N회 → result 1회) 준수
  ② 직행: "리스크 분석해줘" → engine_b 라우팅 → step 번역 → done.answer
  ③ analyze_impact 도구 단위 — stream_writer 로 progress 중계 확인
  ④ 측면: 연차 신청 중 심층 분석 → 분석 step 이 스트림에 섞여 나오고
     이어서 leave.create 승인까지 도달

실행:  uv run python -m app.tests.test_engine_b
"""

from __future__ import annotations

import asyncio
import json
import types
import uuid

import httpx

from app.main import app


def _body(goal: str) -> dict:
    return {
        "runId": f"run_{uuid.uuid4().hex[:8]}",
        "conversationId": 12, "goal": goal, "messages": [],
        "screenContext": {"screen": "HOME", "formState": {}},
        "requestSource": "WEB", "locale": "ko-KR",
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
        # done 훅이 기억 카드용 스토어도 열므로 그쪽도 닫아야 프로세스가 끝난다
        from app.common.checkpoint import close_checkpointer
        from app.memory.store import close_memory_store
        await asyncio.sleep(1)          # 발사 후 망각 태스크 잔여분 소화
        await close_checkpointer()
        await close_memory_store()


async def _scenarios() -> None:
    # ── ① run_engine_b 계약 준수 ─────────────────────────────
    from app.engine_b.runner import run_engine_b

    events = [ev async for ev in run_engine_b("일정이 밀렸는데 위험한가?", "run_test")]
    kinds = [e["type"] for e in events]
    assert kinds.count("result") == 1 and kinds[-1] == "result", kinds
    assert kinds.count("progress") >= 2, f"progress 부족: {kinds}"
    assert len(events[-1]["answer"]) > 20, events[-1]["answer"][:80]
    print(f"✅ ① run_engine_b: progress {kinds.count('progress')}회 → result "
          f"({events[-1]['answer'][:40]!r})", flush=True)

    # ── ③ analyze_impact 도구 — stream_writer 중계 ──────────
    from app.tools.analyze import analyze_impact
    from app.tools.registry import RunContext

    pushed: list = []
    rt = types.SimpleNamespace(context=RunContext(run_id="run_test"),
                               stream_writer=pushed.append, state={},
                               tool_call_id="tc_t", store=None, config={})
    out = await analyze_impact.coroutine(
        question="이하늘이 하루 빠지면 베타 오픈이 위험한가?", runtime=rt)
    assert out.startswith("[심층 분석 결과]"), out[:60]
    assert len(pushed) >= 2, f"progress 중계 안 됨: {pushed}"
    print(f"✅ ③ analyze_impact: 진행 {len(pushed)}건 중계 + 결과 반환", flush=True)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=180) as client:

        # ── ② 직행 — 분석 요청이 engine_b 로 완주 ────────────
        events = await _collect_sse(client, "/api/agent/runs",
                                    _body("그룹웨어 프로젝트 일정이 밀릴 위험이 있는지 "
                                          "시나리오 분석해줘"))
        names = [n for n, _ in events]
        assert names[-1] == "done", names
        assert names.count("step") >= 3, f"엔진 B 진행 step 부족: {names}"
        assert len(events[-1][1]["answer"]) > 20, events[-1][1]
        print(f"✅ ② 직행: {names} → done ({events[-1][1]['answer'][:40]!r})", flush=True)

        # ── ④ 측면 — 연차 신청 중 심층 분석 → 승인 도달 ──────
        body = _body("다음 주 화요일 하루 연차를 내려는데, 프로젝트 일정에 위험한지 "
                     "심층 분석해보고 괜찮으면 신청 진행해줘. 사유는 개인 사정.")
        events = await _collect_sse(client, "/api/agent/runs", body)
        hops = 0
        while events[-1][0] == "question" and hops < 3:
            events = await _collect_sse(client, f"/api/agent/runs/{body['runId']}/resume",
                                        {"questionId": 1, "selectedIds": [],
                                         "text": "다음 주 화요일 하루, ANNUAL, 사유는 개인 사정. 진행해."})
            hops += 1
        names = [n for n, _ in events]
        # "괜찮으면 신청해줘"는 조건부 — 분석 결과에 따라 두 결말 모두 정당하다:
        #   ⓐ 위험 없음 판단 → leave.create 승인 대기
        #   ⓑ 위험 있음 판단 → 신청 보류하고 done 으로 보고 (분석이 판단 재료로 쓰인 증거)
        last, payload = events[-1]
        if last == "approval_request":
            assert payload["tool"] == "leave.create", payload["tool"]
            outcome = "신청 진행 → 승인 대기"
        else:
            assert last == "done" and len(payload["answer"]) > 10, (last, payload)
            outcome = "위험 판단 → 신청 보류 보고"
        steps = [p["text"] for n, p in events if n == "step"]
        analysis_steps = [t for t in steps if "분석" in t or "종합" in t]
        assert analysis_steps, f"분석 진행 step 이 스트림에 없음: {steps}"
        print(f"✅ ④ 측면: 분석 step {len(analysis_steps)}건 중계 → {outcome}", flush=True)

    print("\n엔진 B 결합 관통 성공", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
