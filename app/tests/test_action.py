"""
done.action 관통 테스트 — NAVIGATE · FILL_FORM

시나리오:
  ① 저장 후 NAVIGATE — 회의록 저장 승인 → done.action 이 MEETING_DETAIL
     (meetingId=57, 규격 예시와 같은 모양)
  ② 삭제는 NAVIGATE만 — "회의록 삭제해줘" → 저장·삭제 도구 호출 없이
     기존 회의록(41) 화면으로 안내
  ③ 프로젝트 생성 FILL_FORM — 대화로 정보 수집(A-2) → done.action 이
     FILL_FORM + formData (생성 버튼은 사용자 몫)

실행:  uv run python -m app.tests.test_action
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from app.config import settings
from app.main import app


def _body(goal: str, screen: str = "HOME") -> dict:
    return {
        "runId": f"run_{uuid.uuid4().hex[:8]}",
        "conversationId": 12,
        "goal": goal,
        "messages": [],
        "screenContext": {"screen": screen, "formState": {}},
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


async def _drive_to_done(client, run_id, events, answer_text, max_hops=6):
    """question 은 답하고 approval 은 승인하며 done 까지."""
    for _ in range(max_hops):
        kind, payload = events[-1]
        if kind == "done":
            return payload
        if kind == "question":
            body = {"questionId": 1, "selectedIds": [], "text": answer_text}
        elif kind == "approval_request":
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
                                 headers={"X-Internal-Api-Key": settings.internal_api_key}) as client:

        # ── ① 저장 → NAVIGATE(MEETING_DETAIL) ────────────────
        body = _body("그룹웨어 AI 고도화 프로젝트에 오늘(2026-08-04) 스프린트 리뷰 "
                     "회의록 올려줘. 참석자 김서준·이하늘, 목적 진행 공유, 내용 API 68% 완료.")
        events = await _collect_sse(client, "/api/agent/runs", body)
        done = await _drive_to_done(client, body["runId"], events,
                                    "그룹웨어 AI 고도화, 2026-08-04, 김서준·이하늘, "
                                    "목적 진행 공유, 내용 API 68% 완료")
        action = done["action"]
        assert action and action["type"] == "NAVIGATE", done
        assert action["targetScreen"] == "MEETING_DETAIL", action
        assert action["params"]["meetingId"] == 57, action
        assert action.get("label"), action
        print(f"✅ ① 저장 → NAVIGATE: {action['targetScreen']} {action['params']}", flush=True)

        # ── ② 삭제 = NAVIGATE만 ──────────────────────────────
        body = _body("그룹웨어 AI 고도화 프로젝트의 지난주 스프린트 리뷰 3차 회의록 삭제해줘")
        events = await _collect_sse(client, "/api/agent/runs", body)
        done = await _drive_to_done(client, body["runId"], events,
                                    "그룹웨어 AI 고도화 프로젝트의 '스프린트 리뷰 3차' 회의록")
        action = done["action"]
        assert action and action["type"] == "NAVIGATE", done
        assert action["targetScreen"] == "MEETING_DETAIL", action
        assert action["params"].get("meetingId") == 41, action     # 기존 회의록으로 안내
        print(f"✅ ② 삭제 → NAVIGATE만: {action['params']} (삭제 도구 없음 확인)", flush=True)

        # ── ③ 프로젝트 생성 → FILL_FORM (A-2 대화) ───────────
        # 에이전트는 ask_user(question 이벤트) 또는 done.answer 로 물을 수 있다.
        # done 으로 물으면 다음 발화는 "새 Run + messages 히스토리" 로 이어진다
        # — BE 가 실제로 하는 그대로.
        info = ("프로젝트 이름은 'AI 검색 개선', 기간은 2026-09-01부터 "
                "2026-12-31까지. 예산이랑 멤버는 나중에 정할게. 이제 폼 채워줘.")
        convo: list[dict] = []
        goal = "새 프로젝트 만들고 싶어"
        action = None
        for _ in range(5):
            body = _body(goal)
            body["messages"] = convo
            events = await _collect_sse(client, "/api/agent/runs", body)
            done = await _drive_to_done(client, body["runId"], events, info)
            action = done["action"]
            if action:
                break
            # done.answer 가 질문(A-2) — 대화를 쌓고 새 Run 으로 답한다
            convo += [{"role": "USER", "content": goal},
                      {"role": "AGENT", "content": done["answer"]}]
            goal = info
        assert action and action["type"] == "FILL_FORM", done
        assert action["targetScreen"] == "PROJECT_CREATE", action
        form = action["formData"]
        assert form.get("name") == "AI 검색 개선", form
        assert form.get("startDate") == "2026-09-01", form
        print(f"✅ ③ 프로젝트 생성 → FILL_FORM (A-2 대화): {list(form.keys())} 채움", flush=True)

    print("\ndone.action 관통 성공", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
