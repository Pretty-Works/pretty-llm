"""
메모리·RAG 관통 테스트 — 명세: docs/MEMORY_RAG_SPEC.md §6

시나리오:
  ① 대화 요약 카드 생성 (done 훅, 발사 후 망각)
  ② 증분 확장 — 같은 대화 두 번째 run 후에도 카드 1장 + 내용 갱신
  ③ recall 회상 — 카드가 검색되어 문장으로 나오는가
  ④ 회의록 RAG — 색인 → 내용 검색 → meetingId 포인터 / 프로젝트 격리
  ⑤ 분석 결과 카드 — 엔진 B result 훅
  ⑥ 문서 RAG — txt 업로드 → doc_search 출처 포함
  ⑦ 재시작 생존 — store 재오픈 후 검색 유지

실행:  uv run python -m app.tests.test_memory   (mock 백엔드: userId=5)
"""

from __future__ import annotations

import asyncio
import json
import types
import uuid

import httpx

from app.main import app

CONV_ID = int(uuid.uuid4().int % 10**8)      # 테스트 실행마다 새 대화 (DB 잔존 대비)


def _body(goal: str, conv_id: int = CONV_ID) -> dict:
    return {
        "runId": f"run_{uuid.uuid4().hex[:8]}",
        "conversationId": conv_id, "goal": goal, "messages": [],
        "screenContext": {"screen": "HOME", "formState": {}},
        "requestSource": "WEB", "locale": "ko-KR",
    }


async def _collect_sse(client, url, body):
    events, name = [], None
    async with client.stream("POST", url, json=body) as res:
        assert res.status_code == 200, f"{url} → {res.status_code}"
        async for line in res.aiter_lines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[6:])))
    return events


async def _drive_to_done(client, run_id, events, answer="그룹웨어 AI 고도화, 진행해줘",
                         max_hops=6):
    for _ in range(max_hops):
        kind, payload = events[-1]
        if kind == "done":
            return payload
        if kind == "question":
            body = {"questionId": 1, "selectedIds": [], "text": answer}
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
        from app.common.checkpoint import close_checkpointer
        from app.memory.store import close_memory_store
        await asyncio.sleep(1)                # 발사 후 망각 태스크 잔여분 소화
        await close_checkpointer()
        await close_memory_store()


async def _scenarios() -> None:
    from app.memory.store import get_card, search_cards, close_memory_store
    from app.memory import vectordb

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                 timeout=180) as client:

        # ── ① 요약 카드 생성 (+ ④ 의 색인 재료를 겸해 회의록 저장 run) ──
        body = _body("그룹웨어 AI 고도화 프로젝트에 오늘 '예산 점검 회의' 회의록 올려줘. "
                     "참석자 김서준·정우진, 목적은 예산 조정, "
                     "내용은 외주비 3천만원에서 2천5백으로 감액 결정.")
        done = await _drive_to_done(client, body["runId"],
                                    await _collect_sse(client, "/api/agent/runs", body))
        assert "회의록" in done["answer"], done["answer"]
        await asyncio.sleep(5)                # 백그라운드 요약·색인 완료 대기

        card = await get_card(("conv", 5), str(CONV_ID))
        assert card is not None, "요약 카드가 안 만들어짐"
        assert card["title"] and card["summary"], card
        summary_v1 = card["summary"]
        print(f"✅ ① 요약 카드 생성: {card['title']!r} / {summary_v1[:50]!r}", flush=True)

        # ── ② 증분 확장 — 같은 대화의 두 번째 run ────────────
        body = _body("방금 그 회의록에 이어서, 다음 주 화요일 하루 연차도 신청해줘. "
                     "사유는 개인 사정.", conv_id=CONV_ID)
        await _drive_to_done(client, body["runId"],
                             await _collect_sse(client, "/api/agent/runs", body),
                             answer="다음 주 화요일 하루, ANNUAL, 사유는 개인 사정")
        await asyncio.sleep(5)

        card2 = await get_card(("conv", 5), str(CONV_ID))
        assert card2 is not None and card2["summary"] != summary_v1, "증분 확장 안 됨"
        print(f"✅ ② 증분 확장: 카드 1장 유지, 내용 갱신 ({card2['summary'][:60]!r})",
              flush=True)

        # ── ③ recall 회상 (도구 단위 — 결정적) ───────────────
        from app.tools.memory_tool import recall, doc_search
        from app.tools.registry import RunContext

        rt = types.SimpleNamespace(context=RunContext(run_id="run_recall_t"),
                                   stream_writer=lambda _: None, state={},
                                   tool_call_id="tc", store=None, config={})
        out = await recall.coroutine(query="예전에 예산 관련해서 했던 일", runtime=rt)
        assert "[대화" in out and str(CONV_ID) in out, out[:300]
        print(f"✅ ③ recall: 대화 카드 회상 ({out.splitlines()[1][:70]!r})", flush=True)

        # ── ④ 회의록 RAG — 내용 검색 + 격리 ──────────────────
        assert "[회의록" in out and "meetingId=57" in out, \
            f"회의록 색인 검색 실패: {out[:300]}"
        isolated = await vectordb.search_meetings("예산 감액", [999], k=3)
        assert isolated == [], "프로젝트 격리 실패 — 남의 프로젝트로 검색됨"
        print("✅ ④ 회의록 RAG: 내용 검색 → meetingId 포인터 + 프로젝트 격리", flush=True)

        # ── ⑤ 분석 결과 카드 ─────────────────────────────────
        from app.engine_b.runner import run_engine_b
        async for _ev in run_engine_b("외주비 감액이 일정에 위험한가?", "run_ana_t"):
            pass
        await asyncio.sleep(2)
        hits = await search_cards(("analyses", 5), "외주비 감액 위험", limit=3)
        assert hits, "분석 카드가 저장 안 됨"
        print(f"✅ ⑤ 분석 카드: {hits[0].value['title']!r}", flush=True)

        # ── ⑥ 문서 RAG — txt 업로드 → doc_search ─────────────
        rule_txt = ("연차 규정 제3조: 연차(ANNUAL)는 연 15일 부여하며 사용 시 차감한다. "
                    "제4조: 공가(EXCUSED)는 경조사·예비군 등 공적 사유에 부여하며 "
                    "연차에서 차감하지 않는다. "
                    "제5조: 연차 신청은 사용일 3일 전까지 함을 원칙으로 한다.").encode()
        res = await client.post("/docs", files={"file": ("연차규정.txt", rule_txt,
                                                         "text/plain")})
        assert res.status_code == 200 and res.json()["chunks"] >= 1, res.text

        out = await doc_search.coroutine(query="공가는 연차에서 차감되나요?", runtime=rt)
        assert "연차규정.txt" in out and "차감하지 않는다" in out, out[:300]
        print("✅ ⑥ 문서 RAG: txt 업로드 → 출처 포함 검색", flush=True)

        # ── ⑦ 재시작 생존 — store 재오픈 ─────────────────────
        await close_memory_store()
        card3 = await get_card(("conv", 5), str(CONV_ID))
        assert card3 is not None, "재오픈 후 카드 유실"
        hits = await search_cards(("conv", 5), "예산 회의", limit=3)
        assert hits, "재오픈 후 의미검색 실패"
        print("✅ ⑦ 재시작 생존: store 재오픈 후 카드·의미검색 유지", flush=True)

    print("\n메모리·RAG 관통 성공", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
