"""
신규 도메인 에이전트 3종 관통 — 할일(배치) · 일정 · 지출

각 도메인이 라우팅 → 도구 선택 → 승인 → 실행 → done 을 완주하는지 확인한다.

실행:  uv run python -m app.tests.test_domains
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

# ★ 회귀는 항상 mock 으로 돈다 — .env 가 MOCK_BACKEND=false 여도 강제한다.
#   이 스위트들은 승인까지 태워 실제로 저장을 실행하므로(회의록·연차·할일),
#   실 BE 를 보게 두면 테스트를 돌릴 때마다 진짜 데이터가 쌓이고 연차는
#   승인자에게 알림까지 나간다. conftest 는 pytest 전용이라 여기엔 안 걸린다.
import os  # noqa: E402
os.environ["MOCK_BACKEND"] = "true"

from app.config import settings
from app.main import app


def _body(goal: str) -> dict:
    return {
        "runId": f"run_{uuid.uuid4().hex[:8]}",
        "conversationId": 12,
        "goal": goal,
        "messages": [],
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


async def _drive(client, run_id, events, answer, max_hops=6):
    """question 은 답하고 approval 은 승인하며 done 까지. 승인된 tool 들을 기록."""
    approved = []
    for _ in range(max_hops):
        kind, payload = events[-1]
        if kind == "done":
            return payload, approved
        if kind == "question":
            body = {"questionId": 1, "selectedIds": [], "text": answer}
        elif kind == "approval_request":
            approved.append(payload)
            body = {"toolCallId": payload["toolCallId"], "decision": "APPROVED",
                    "approvalToken": "apv_test"}
        else:
            raise AssertionError(f"비정상 이벤트: {kind}")
        events = await _collect_sse(client, f"/api/agent/runs/{run_id}/resume", body)
    raise AssertionError("done 도달 실패")


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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=120,
                                 headers={"X-Internal-Api-Key": settings.inbound_api_key}) as client:

        # ── ① 할일 — 여러 건이 배치 1회 승인으로 ────────────
        body = _body("그룹웨어 프로젝트에 할일 두 개 등록해줘: "
                     "'API 명세 문서화' 는 2026-08-07 까지, '연동 테스트' 는 2026-08-08 까지.")
        events = await _collect_sse(client, "/api/agent/runs", body)
        done, approved = await _drive(client, body["runId"], events,
                                      "그룹웨어 AI 고도화 프로젝트. 마감일은 말한 그대로.")
        task_approvals = [a for a in approved if a["tool"] == "task.create"]
        assert task_approvals, f"task.create 승인이 없음: {[a['tool'] for a in approved]}"
        assert len(task_approvals) == 1, f"배치가 안 됨 — 승인 {len(task_approvals)}회"
        assert len(task_approvals[0]["params"]["tasks"]) == 2, task_approvals[0]["params"]
        print(f"✅ ① 할일 배치: 2건이 승인 1회로 → {done['answer'][:40]!r}", flush=True)

        # ── ② 일정 — 겹침 확인 후 생성 ──────────────────────
        body = _body("다음 주 화요일 오후 2시에 김서준님과 한 시간 팀미팅 잡아줘")
        events = await _collect_sse(client, "/api/agent/runs", body)
        done, approved = await _drive(client, body["runId"], events,
                                      "다음 주 화요일 맞아요. 제목은 '팀미팅'으로.")
        sched = [a for a in approved if a["tool"] == "schedule.create"]
        assert sched, f"schedule.create 승인이 없음: {[a['tool'] for a in approved]}"
        p = sched[0]["params"]
        assert p["startAt"].startswith("2026-08-11"), f"상대 날짜 계산 오류: {p['startAt']}"
        assert p["type"] != "LEAVE", p
        print(f"✅ ② 일정: user_me 기준 날짜 계산(8/11 화) → 승인 → {done['answer'][:40]!r}",
              flush=True)

        # ── ③ 지출 — 금액 원 단위 해석 ──────────────────────
        body = _body("그룹웨어 프로젝트에 어제 회식비 12만원 지출 등록해줘. 사용처는 한경식당.")
        events = await _collect_sse(client, "/api/agent/runs", body)
        done, approved = await _drive(client, body["runId"], events,
                                      "목적은 팀 회식이야. 분류는 식비.")
        exp = [a for a in approved if a["tool"] == "expense.create"]
        assert exp, f"expense.create 승인이 없음: {[a['tool'] for a in approved]}"
        p = exp[0]["params"]
        assert p["amount"] == 120000, f"금액 해석 오류: {p['amount']}"
        assert p["expenseDate"] == "2026-08-04", f"'어제' 계산 오류(user_me 미사용?): {p['expenseDate']}"
        print(f"✅ ③ 지출: '12만원'→120000, '어제'→2026-08-04 → {done['answer'][:40]!r}", flush=True)

    print("\n신규 도메인 3종 관통 성공", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
