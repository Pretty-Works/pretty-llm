"""
복합 요청(다중 도메인) 관통 테스트

시나리오:
  ① classify 가 복합 요청에서 도메인을 전부 잡아내는가 (vacation + meeting)
  ② 복합 관통 — 연차 신청(승인 필요) + 회의록 확인(조회)이 한 Run 에서:
     seg1: 분해 → 연차 에이전트 → approval_request 로 멈춤 (계획 저장 확인)
     seg2: 승인 → 연차 완료 → 회의 에이전트로 릴레이 → done (결과 종합)
  ③ 단독 연차 요청 — 도메인 기반 에이전트 선택 + metadata(domain) 복원 resume

실행:  uv run python -m app.tests.test_composite
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from app.config import settings
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


async def _resume_until_terminal(client, run_id, first_events, answer_text,
                                 approve=True, max_hops=5):
    """question 이면 답하고, approval 이면 승인하며 done 까지 진행한다."""
    events = first_events
    for _ in range(max_hops):
        kind, payload = events[-1]
        if kind == "done":
            return events
        if kind == "question":
            body = {"questionId": 1, "selectedIds": [], "text": answer_text}
        elif kind == "approval_request":
            if not approve:
                return events
            body = {"toolCallId": payload["toolCallId"], "decision": "APPROVED",
                    "approvalToken": "apv_test"}
        else:
            raise AssertionError(f"비정상 종료 이벤트: {kind}")
        events = await _collect_sse(client, f"/api/agent/runs/{run_id}/resume", body)
    raise AssertionError("max_hops 안에 done 에 못 도달")


GOAL_COMPOSITE = ("2026-08-11 하루 연차 신청해줘. 사유는 개인 사정이야. "
                  "그리고 그룹웨어 프로젝트의 회의록 목록도 보여줘.")
ANSWER = ("2026-08-11 하루 연차, 종류는 일반 연차(ANNUAL), 사유는 개인 사정. "
          "회의록은 그룹웨어 AI 고도화 프로젝트 것.")


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
    # ── ① classify 다중 도메인 감지 ──────────────────────────
    from app.orchestrator.classify import classify

    d = await classify("다음주 화요일에 연차 내고, 그 기간에 잡힌 회의도 확인해줘")
    assert d.route == "engine_a", d
    assert set(d.domains) >= {"vacation", "meeting"}, f"도메인 누락: {d.domains}"
    print(f"✅ ① classify 복합 감지: route={d.route}, domains={d.domains}", flush=True)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=120,
                                 headers={"X-Internal-Api-Key": settings.internal_api_key}) as client:

        # ── ② 복합 관통 (연차 신청 + 회의록 확인) ────────────
        body = _body(GOAL_COMPOSITE)
        run_id = body["runId"]
        events = await _collect_sse(client, "/api/agent/runs", body)
        names = [n for n, _ in events]

        # seg1 은 연차 승인에서 멈춰야 한다 (question 경유 허용)
        from app.orchestrator.composite import load_plan
        while events[-1][0] == "question":
            events = await _collect_sse(client, f"/api/agent/runs/{run_id}/resume",
                                        {"questionId": 1, "selectedIds": [], "text": ANSWER})
        assert events[-1][0] == "approval_request", f"승인 대기 아님: {names}"
        assert events[-1][1]["tool"] == "leave.create", events[-1][1]
        plan = await load_plan(run_id)
        assert plan is not None and plan["current"] == 0, plan
        assert len(plan["subtasks"]) >= 2, plan["subtasks"]
        print(f"✅ ② seg1: 분해 {len(plan['subtasks'])}개 → 연차 승인 대기 (계획 영속 확인)",
              flush=True)

        # seg2: 승인 → 연차 완료 → 회의 작업 릴레이 → done
        events = await _resume_until_terminal(client, run_id, events, ANSWER)
        done = events[-1][1]
        assert "연차" in done["answer"] or "leaveId" in done["answer"], done
        assert "회의" in done["answer"] or "41" in done["answer"], done
        plan = await load_plan(run_id)
        assert plan["current"] == len(plan["subtasks"]), f"완료 마킹 안 됨: {plan}"
        print(f"✅ ② seg2: 승인 → 릴레이 → done (종합 답변: {done['answer'][:60]!r})",
              flush=True)

        # ── ③ 단독 연차 — 도메인 에이전트 선택 + resume 복원 ──
        body = _body("2026-08-12 하루 연차 신청해줘. 일반 연차고 사유는 병원 방문이야.")
        events = await _collect_sse(client, "/api/agent/runs", body)
        while events[-1][0] == "question":
            events = await _collect_sse(client, f"/api/agent/runs/{body['runId']}/resume",
                                        {"questionId": 1, "selectedIds": [], "text": "2026-08-12 하루, ANNUAL, 사유는 병원 방문"})
        assert events[-1][0] == "approval_request", [n for n, _ in events]
        assert events[-1][1]["tool"] == "leave.create", events[-1][1]

        events = await _resume_until_terminal(client, body["runId"], events, "")
        assert "연차" in events[-1][1]["answer"], events[-1][1]
        print("✅ ③ 단독 연차: 도메인 선택 → 승인 → done (metadata 복원 경유)", flush=True)

    print("\n복합 요청 관통 성공", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
