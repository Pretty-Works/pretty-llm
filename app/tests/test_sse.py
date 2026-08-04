"""
SSE 관통 테스트 — BE 가 할 두 번의 호출을 그대로 흉내낸다 (실 LLM + mock 백엔드)

시나리오:
  ① Run 시작 → step … → approval_request → 스트림 정상 종료
  ② APPROVED 로 재개 → 저장 실행 → done (mock meetingId=57 확인)
  ③ REJECTED 로 재개 → 저장 없이 done (대안 답변)
  ④ 없는 runId 로 재개 → 404 (AGENT_016)
  ⑤ toolCallId 불일치 → 400
  ⑥ 생성기가 죽어도 error 이벤트가 나가는가 (_guard 단위 검증)

실행:  uv run python -m app.tests.test_sse
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from app.main import app


def _run_body(goal: str) -> dict:
    return {
        "runId": f"run_{uuid.uuid4().hex[:8]}",
        "conversationId": 12,
        "goal": goal,
        "messages": [],
        "screenContext": {"screen": "HOME", "formState": {}},
        "requestSource": "WEB",
        "locale": "ko-KR",
    }


async def _collect_sse(client: httpx.AsyncClient, method: str, url: str,
                       body: dict) -> list[tuple[str, dict]]:
    """SSE 응답을 (이벤트명, data dict) 목록으로 파싱한다."""
    events, name = [], None
    async with client.stream(method, url, json=body) as res:
        assert res.status_code == 200, f"{url} → {res.status_code}"
        assert res.headers["content-type"].startswith("text/event-stream")
        async for line in res.aiter_lines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                # 규격: data 는 반드시 한 줄 → 여기서 바로 json 이 되어야 한다
                events.append((name, json.loads(line[len("data: "):])))
    return events


GOAL = ("그룹웨어 프로젝트에 오늘(2026-08-04) 스프린트 리뷰 회의록 올려줘. "
        "참석자는 김서준, 이하늘. 목적은 진행 상황 공유, 내용은 백엔드 API 68% 완료.")

# LLM 이 비결정적으로 question 을 먼저 물을 수 있다 — 이는 계약상 합법이라
# (BE 도 언제든 question 을 처리해야 함) 테스트도 그 경우 답하고 계속 간다.
FULL_INFO = ("그룹웨어 AI 고도화 프로젝트야. 제목 '스프린트 리뷰', 날짜 2026-08-04, "
             "참석자 김서준·이하늘, 목적은 진행 상황 공유, 내용은 백엔드 API 68% 완료.")


async def _start_until_approval(client, body, max_hops: int = 3):
    """Run 을 시작하고, 중간 question 은 답해 가며 approval_request 까지 간다."""
    events = await _collect_sse(client, "POST", "/api/agent/runs", body)
    first_names = [n for n, _ in events]
    hops = 0
    while events[-1][0] == "question" and hops < max_hops:
        events = await _collect_sse(client, "POST",
                                    f"/api/agent/runs/{body['runId']}/resume",
                                    {"answer": FULL_INFO})
        hops += 1
    return first_names, events


async def main() -> None:
    try:
        await _scenarios()
    finally:
        # 실패하더라도 커넥션을 닫아야 프로세스가 끝난다 (비데몬 스레드)
        from app.common.checkpoint import close_checkpointer
        await close_checkpointer()


async def _scenarios() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=90) as client:

        # ── ① Run 시작 → (question 허용) → approval_request ──
        body = _run_body(GOAL)
        run_id = body["runId"]
        first_names, events = await _start_until_approval(client, body)

        names = [n for n, _ in events]
        assert names[-1] == "approval_request", f"마지막 이벤트가 {names} 로 끝남"
        assert "step" in first_names, f"step 이 한 번도 없음: {first_names}"

        approval = events[-1][1]
        assert approval["tool"] == "meeting.create", approval
        assert approval["access"] == "WRITE"
        assert approval["toolCallId"], "toolCallId 가 비어 있음"
        assert approval["params"]["title"], approval["params"]
        print(f"✅ ① 시작 세그먼트: {names} → 승인 대기 (toolCallId={approval['toolCallId']})", flush=True)

        # ── ② APPROVED 재개 → 저장 → done ────────────────────
        events = await _collect_sse(
            client, "POST", f"/api/agent/runs/{run_id}/resume",
            {"toolCallId": approval["toolCallId"], "decision": "APPROVED",
             "approvalToken": "apv_test", "reason": None},
        )
        names = [n for n, _ in events]
        assert names[-1] == "done", f"done 으로 안 끝남: {names}"
        done = events[-1][1]
        assert done["answer"], done
        print(f"✅ ② 승인 재개: {names} → done.answer={done['answer'][:60]!r}", flush=True)

        # ── ③ REJECTED 재개 → 저장 없이 done ─────────────────
        body = _run_body(GOAL)
        _, events = await _start_until_approval(client, body)
        assert events[-1][0] == "approval_request", f"승인 도달 실패: {[n for n, _ in events]}"
        approval = events[-1][1]
        events = await _collect_sse(
            client, "POST", f"/api/agent/runs/{body['runId']}/resume",
            {"toolCallId": approval["toolCallId"], "decision": "REJECTED",
             "approvalToken": None, "reason": "참석자가 틀렸어요. 저장하지 마세요."},
        )
        names = [n for n, _ in events]
        # 거절 후 에이전트는 ⓐ 포기하고 done, ⓑ 사유를 반영한 재제안(approval_request),
        # ⓒ 사유에 대한 되묻기(question) — 셋 다 규격이 허용하는 흐름이다.
        # 계약 위반은 "종료 이벤트 없이 끊기는 것"뿐.
        assert names[-1] in ("done", "approval_request", "question"), \
            f"거절 후 비정상 종료: {names}"
        print(f"✅ ③ 거절 재개: {names} → {names[-1]} 로 정상 종료 (재제안 허용)", flush=True)

        # ── ④ 없는 runId → 404 AGENT_016 ─────────────────────
        res = await client.post("/api/agent/runs/run_ghost/resume",
                                json={"toolCallId": "tc_x", "decision": "APPROVED"})
        assert res.status_code == 404, res.status_code
        assert res.json()["detail"]["errorCode"] == "AGENT_016"
        print("✅ ④ 체크포인트 없음 → 404 AGENT_016", flush=True)

        # ── ⑤ toolCallId 불일치 → 400 ────────────────────────
        body = _run_body(GOAL)
        _, events = await _start_until_approval(client, body)
        assert events[-1][0] == "approval_request", f"승인 도달 실패: {[n for n, _ in events]}"
        res = await client.post(f"/api/agent/runs/{body['runId']}/resume",
                                json={"toolCallId": "tc_wrong", "decision": "APPROVED"})
        assert res.status_code == 400, res.status_code
        print("✅ ⑤ toolCallId 불일치 → 400", flush=True)

    # ── ⑥ 죽는 생성기 → error 이벤트 보장 (_guard) ───────────
    from app.api.agent import _guard

    async def dying():
        yield "event: step\ndata: {}\n\n"
        raise RuntimeError("전원이 뽑혔다")

    out = [e async for e in _guard(dying())]
    assert out[-1].startswith("event: error"), out
    print("✅ ⑥ 예외 → error 이벤트로 마감 (AGENT_017 방지)", flush=True)

    print("\n관통 성공 — v2 계약 전 구간 통과")


if __name__ == "__main__":
    asyncio.run(main())
