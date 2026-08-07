# app/engine_b/entry.py
"""
Engine B replan 진입 — api/agent.py 가 engine_b 로 라우팅한 요청을 replan 흐름으로 잇는다.

★ 훅 위치가 중요하다 — classify 보다 '먼저' 대기 제안을 확인해야 한다.
  선택 메시지("2번")는 그 자체론 engine_b 로 안 분류되므로, engine_b 분기 안에서
  잡으려 하면 도달조차 못 한다. start_run 맨 앞에서 route 와 무관하게 가로챈다:

    # api/agent.py  start_run 맨 앞
    if replan_entry.is_replan_selection(req):                   # ① 분류보다 먼저(선택 이어받기)
        gen = replan_entry.stream_start(req, ctx.approval_token)
        return StreamingResponse(_guard(gen), ...)
    decision = await classify(...)                              # ② 아니면 평소대로 분류
    if decision.route == "engine_b" and decision.mode == "replan":
        gen = replan_entry.stream_start(req, ctx.approval_token)   # 새 생성(턴1)

동작(턴 판별 — stream_start 내부):
  · 이 대화에 대기 중 제안이 있고, 메시지가 '선택'으로 읽히면  → 반영(턴2)
  · 아니면                                                     → 3안 생성(턴1)

★ 반영(WRITE)은 승인 토큰이 필요하다. 토큰이 있으면 바로 반영하고, 없으면
  approval_request 를 내보낸다 — 그 뒤 BE 가 토큰을 실어 /resume 하는 choreography(어느
  대화·어느 안인지 복원하는 방식)는 담당자1 HITL 규격에 맞춰 잇는다.
  데모는 autoApprove/더미 토큰으로 토큰을 채워 한 턴에 반영할 수 있다.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from app.common import sse
from app.common.exceptions import ApprovalRequiredError
from app.engine_b.replan_service import apply_from_text, parse_selection, propose
from app.engine_b.suggestion_store import store
from app.schemas.state import AnalysisRequest, UIContext


async def stream_start(req, approval_token: str | None = None) -> AsyncIterator[str]:
    """engine_b(replan) 새 턴. 선택이면 반영, 아니면 생성."""
    conv, run_id = str(req.conversationId), req.runId

    pending = store.load(conv)
    if pending and parse_selection(req.goal, pending.scenario_types):
        # ── 턴2: 선택 → 반영 ──
        yield sse.step("선택한 재계획을 반영하는 중...")
        try:
            answer = await apply_from_text(conv, req.goal, run_id, approval_token)
        except ApprovalRequiredError:
            # 토큰 없음 → 승인 요청. (payload 최종형은 담당자1 HITL 규격에 맞춘다)
            yield sse.sse_event("approval_request", {
                "tool": "replan.apply",
                "access": "WRITE",
                "summary": "재계획 반영을 승인해 주세요",
            })
            return
        yield sse.sse_event("done", {"answer": answer, "action": None})
        return

    # ── 턴1: 3안 생성 ──
    yield sse.step("재계획 방안을 분석하는 중...")
    answer = await propose(_to_request(req), conv, run_id)
    yield sse.sse_event("done", {"answer": answer, "action": None})


def is_replan_selection(req) -> bool:
    """대기 제안이 있고 메시지가 선택으로 읽히나 — api 훅에서 라우팅 판단용."""
    pending = store.load(str(req.conversationId))
    return bool(pending and parse_selection(req.goal, pending.scenario_types))


def _to_request(req) -> AnalysisRequest:
    """RunRequest → Engine B 입력.

    ★ user_id: 내부 규격상 요청 바디엔 userId 가 없다(X-Run-Id 로 BE 가 역산).
      Engine B 컨텍스트 수집에 요청자 본인이 필요하면 run 컨텍스트나 /me 조회로 채운다.
      여기선 화면 폼값에 있으면 쓰고 없으면 0(권한 판정은 어차피 BE 몫).
    """
    fs = req.screenContext.formState or {}
    return AnalysisRequest(
        query=req.goal,
        user_id=int(fs.get("userId") or 0),
        ui_context=UIContext(
            screen=req.screenContext.screen,
            project_id=fs.get("projectId"),
        ),
    )
