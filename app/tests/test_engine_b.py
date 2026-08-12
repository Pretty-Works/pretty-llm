"""
엔진 B 결합 관통 — 직행(오케스트레이터) · 측면(엔진 A 도구) · 진행 중계 · replan HITL

시나리오:
  ① run_engine_b 단독 — 계약(progress N회 → result 1회) 준수
  ② 직행: "리스크 분석해줘" → engine_b 라우팅 → step 번역 → done.answer
  ③ analyze_impact 도구 단위 — stream_writer 로 progress 중계 확인
  ④ 측면: 연차 신청 중 심층 분석 → 분석 step 이 스트림에 섞여 나오고
     이어서 leave.create 승인까지 도달
  ⑤ replan HITL: 재계획 요청 → engine_b 가 replan 모드로 분기 → ask_user 로
     3안 제시 → 하나 선택 → replan_save 승인 요청 → 승인 → replan_apply 승인
     요청 → 승인 → done 반영 완료 (★ 2026-08-09 BE 스펙 개정 — 저장도 승인
     대상이라 승인이 2회로 늘었다)

실행:  uv run python -m app.tests.test_engine_b
"""

from __future__ import annotations

import asyncio
import json
import types
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

    # ★ app/common/auth.py의 verify_internal_api_key — .env에 INBOUND_API_KEY가
    #   채워진 순간부터 /api/agent/** 가 이 헤더 없인 401을 낸다(BE 흉내). 키가
    #   비어 있으면 auth 쪽이 검증 자체를 건너뛰므로 이 헤더를 늘 보내도 안전하다.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                 timeout=180,
                                 headers={"X-Internal-Api-Key": settings.inbound_api_key}) as client:

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

        # ── ⑤ replan HITL — 3안 생성 → ask_user 선택 → 저장 승인 → 반영 승인 ──
        # (지연 회복 방향 문구를 쓴다 — scenario_executor.build_scenario_specs() 의
        #  EXTEND 안이 항상 "마감 +2주"로 고정돼 있어서, "앞당겨야" 류 문구를 쓰면
        #  방향이 반대인 안이 나온다. 이건 3안생성 파이프라인 자체의 기존 이슈라
        #  이번 범위에서는 안 건드리고, 테스트 쿼리를 파이프라인 설계 의도에 맞춰
        #  지연 회복 쪽으로 맞춘다.)
        body = _body("그룹웨어 AI 고도화 프로젝트 일정이 2주 정도 밀릴 것 같아. "
                     "어떻게 조정하면 좋을지 안 몇 개 뽑아줘")
        body["screenContext"] = {"screen": "PROJECT_DETAIL", "formState": {"projectId": 3}}
        events = await _collect_sse(client, "/api/agent/runs", body)
        names = [n for n, _ in events]
        assert names[-1] == "question", f"3안 제시 후 question 이 아님: {names} / {events[-1]}"
        q = events[-1][1]
        options = q.get("options", [])
        assert options, f"3안 옵션이 비어 있음: {q}"
        print(f"✅ ⑤-1 replan 3안 생성 → 선택 질문: {[o['label'] for o in options]}", flush=True)

        # 첫 번째 안을 선택
        events = await _collect_sse(client, f"/api/agent/runs/{body['runId']}/resume",
                                    {"questionId": 1, "selectedIds": [options[0]["id"]],
                                     "text": options[0]["label"]})
        names = [n for n, _ in events]
        # 선택 직후엔 저장(replan.save) 승인이 먼저 뜬다 — 혹시 재확인 질문이 한 번
        # 더 왔다면 같은 선택으로 한 번 더 밀어준다.
        hops = 0
        while names[-1] == "question" and hops < 2:
            events = await _collect_sse(client, f"/api/agent/runs/{body['runId']}/resume",
                                        {"questionId": 1, "selectedIds": [options[0]["id"]],
                                         "text": options[0]["label"]})
            names = [n for n, _ in events]
            hops += 1
        assert names[-1] == "approval_request", f"선택 후 approval_request 가 아님: {names} / {events[-1]}"
        approval = events[-1][1]
        assert approval["tool"] == "replan.save", approval["tool"]
        assert approval["access"] == "WRITE", approval
        print(f"✅ ⑤-2 선택 → 저장 승인 요청: tool={approval['tool']} summary={approval['summary']!r}",
              flush=True)

        # 저장 승인 → replan_save 실행 → 곧바로 반영(replan.apply) 승인 요청까지
        events = await _collect_sse(client, f"/api/agent/runs/{body['runId']}/resume",
                                    {"toolCallId": approval["toolCallId"], "decision": "APPROVED",
                                     "approvalToken": "test-approval-token"})
        names = [n for n, _ in events]
        assert names[-1] == "approval_request", f"저장 승인 후 approval_request 가 아님: {names} / {events[-1]}"
        approval2 = events[-1][1]
        assert approval2["tool"] == "replan.apply", approval2["tool"]
        assert approval2["access"] == "WRITE", approval2
        print(f"✅ ⑤-3 저장 완료 → 반영 승인 요청: tool={approval2['tool']} "
              f"summary={approval2['summary']!r}", flush=True)

        # 반영 승인 → 실제 반영(mock)까지
        events = await _collect_sse(client, f"/api/agent/runs/{body['runId']}/resume",
                                    {"toolCallId": approval2["toolCallId"], "decision": "APPROVED",
                                     "approvalToken": "test-approval-token"})
        names = [n for n, _ in events]
        assert names[-1] == "done", f"승인 후 done 이 아님: {names} / {events[-1]}"
        assert len(events[-1][1]["answer"]) > 0, events[-1][1]
        print(f"✅ ⑤-4 승인 → 반영 완료: {events[-1][1]['answer'][:60]!r}", flush=True)

    print("\n엔진 B 결합 관통 성공", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
