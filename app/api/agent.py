"""
에이전트 전용 API — 규격 v2 진입점 (BE ↔ FastAPI)

POST /api/agent/runs                : Run 시작. goal 실행 → SSE 스트림
POST /api/agent/runs/{runId}/resume : 승인/거절/답변 후 재개 → SSE 스트림

두 엔드포인트 다 응답은 text/event-stream 이다. 팀원들의 /api/v1/** (REST)
와 별개 계약이라 prefix 를 공유하지 않는다.

라우팅:
  classify → simple_query | engine_a | engine_b  (+ 관련 도메인 목록)
  engine_a 도메인 1개  → 그 도메인 에이전트로 직행
  engine_a 도메인 2개+ → decompose 로 작업 분해 → composite 가 릴레이 실행
  resume 은 goal 이 없으므로: 복합이면 계획 테이블, 단독이면 체크포인트
  metadata(route·domain)에서 "어디로 돌아갈지"를 복원한다.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.common import hitl, sse
from app.common.checkpoint import get_checkpointer
from app.orchestrator import composite, simple_query
from app.orchestrator.classify import classify
from app.orchestrator.domains import agent_for_domain
from app.schemas.state import Mode
from app.tools.registry import RunContext

# ★ app.engine_b.replan_agent 는 여기서 top-level import 하지 않는다 — 그 import
#   체인이 app.engine_b.scenario_executor → graph → app.workers.registry →
#   workers.meeting.project_fit_agent 까지 이어지는데, 그 파일이 모듈 최상단에서
#   ChatOpenAI(...)를 즉시 생성한다(LLM API 키가 없으면 import 시점에 그대로 죽는다).
#   _engine_b_stream() 이 runner.run_engine_b 를 함수 안에서 지연 import 하는 것과
#   같은 이유로, 여기도 실제로 engine_b 요청이 들어올 때만 늦게 불러온다 — 그래야
#   app.main import(=서버 기동, 테스트 collection)이 LLM 키 유무와 무관해진다.

router = APIRouter(prefix="/api/agent", tags=["agent"])

# 프록시가 SSE 를 버퍼링하지 않도록 하는 표준 헤더 묶음
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ── 요청 모델 (규격 v2 — 필드명 camelCase 그대로) ──────────
class ScreenContext(BaseModel):
    screen: str = "HOME"
    formState: dict = Field(default_factory=dict)


class MessageItem(BaseModel):
    role: str            # USER | AGENT
    content: str


class AttachmentItem(BaseModel):
    """채팅 파일 첨부 — BE 가 txt 에서 추출한 텍스트. (규격 v2 확장 제안, BE 합의 전 선택 필드)"""
    name: str = "첨부파일"
    content: str


class RunRequest(BaseModel):
    runId: str           # BE 가 발급. 체크포인트 thread_id 로 그대로 쓴다
    conversationId: int
    goal: str
    messages: list[MessageItem] = Field(default_factory=list)   # 최근 10건
    attachments: list[AttachmentItem] = Field(default_factory=list)  # 채팅 첨부 (txt 텍스트)
    screenContext: ScreenContext = Field(default_factory=ScreenContext)
    requestSource: str = "WEB"
    locale: str = "ko-KR"
    # 정보용 — 동작 분기 금지 (규격 §5-1). true 여도 approval_request 를 그대로
    # 방출한다. 쓸 수 있는 곳: step 문구 조정, alternatives 생성 생략.
    autoApprove: bool = False


class ResumeRequest(BaseModel):
    """승인 재개와 질문 답변 재개를 한 몸으로 받는다 (규격 v2 §5 그대로).

    approval 대기: toolCallId·decision(APPROVED|REJECTED|ALTERNATIVE) 필수
    question 대기: questionId·selectedIds·text — 검증은 대기 상태를 보고 한다.
    """
    # 승인 재개용
    toolCallId: str | None = None
    decision: str | None = None          # APPROVED | REJECTED | ALTERNATIVE
    approvalToken: str | None = None     # WRITE 승인일 때만. X-Approval-Token 으로 전달됨
    paramsCanonical: str | None = None   # BE 가 해시한 그 문자열 — 바이트 그대로 전송
    reason: str | None = None            # 거절 사유. 선택, 200자 이하
    alternativeId: str | None = None     # decision=ALTERNATIVE 일 때만
    # 질문 답변용
    questionId: int | None = None        # BE 가 부여한 질문 ID
    selectedIds: list[str] = Field(default_factory=list)   # 고른 options[].id
    text: str | None = None              # 자유 입력값 (500자 이하)
    # 공통 — 정보용, 동작 분기 금지. 매 resume 마다 현재값이 온다
    autoApprove: bool = False


# ── 엔드포인트 ─────────────────────────────────────────────
@router.post("/runs")
async def start_run(req: RunRequest) -> StreamingResponse:
    ctx = RunContext(run_id=req.runId,
                     conversation_id=req.conversationId, goal=req.goal,
                     attachments=[a.model_dump() for a in req.attachments] or None)
    history = [m.model_dump() for m in req.messages]
    decision = await classify(req.goal, req.screenContext.screen, history)

    # 화면 맥락을 goal 에 얹는다 — PROJECT_CREATE 화면의 실시간 폼 채움이
    # 이 정보로 동작한다 (이미 입력된 필드는 다시 묻지 않기).
    goal = req.goal
    if req.screenContext.screen != "HOME" or req.screenContext.formState:
        goal = (f"(현재 화면: {req.screenContext.screen}, "
                f"입력된 폼 값: {req.screenContext.formState})\n{req.goal}")
    # 첨부는 본문이 길어 컨텍스트에 싣지 않는다 — 존재만 알리고 도구가 ctx 에서 읽는다.
    if req.attachments:
        names = ", ".join(a.name for a in req.attachments)
        goal += f"\n(첨부 파일 {len(req.attachments)}개: {names} — 내용은 도구가 읽는다)"

    if decision.route == "engine_b":
        # engine_b 안에서 replan(3안 생성 HITL)과 일반 분석(단발)이 갈린다 —
        # analysis_router 의 LLM 1콜(mode 판단)로 여기서 미리 가른다.
        from app.engine_b import entry as engine_b_entry
        from app.engine_b import replan_agent

        plan = await engine_b_entry.route_engine_b(req)
        if plan.mode == Mode.replan:
            agent = await replan_agent.get_agent()
            gen = hitl.stream_run(agent, goal, history, req.runId, ctx,
                                  route="engine_b", domain="replan")
        else:
            gen = _engine_b_stream(goal, req.runId, history)

    elif decision.route == "simple_query":
        agent = await simple_query.get_agent()
        gen = hitl.stream_run(agent, goal, history, req.runId, ctx,
                              route="simple_query")

    else:                                            # engine_a
        domains = decision.domains or ["meeting"]
        subtasks = (await composite.decompose(goal, domains)
                    if len(domains) > 1 else [])

        if len(subtasks) > 1:                        # 복합 — 릴레이 실행
            # conversationId·goal 도 계획에 실어둔다 — 복합은 체크포인트 metadata 가
            # 하위 스레드에만 있어, resume 때 요약 훅이 쓸 값을 여기서 복원한다.
            plan = {"subtasks": subtasks, "current": 0, "answers": [],
                    "conversationId": req.conversationId, "goal": req.goal}
            await composite.save_plan(req.runId, plan)
            gen = composite.stream_composite(req.runId, ctx, plan)
        else:                                        # 단독 — 도메인 에이전트 직행
            domain = domains[0]
            agent = await agent_for_domain(domain)
            gen = hitl.stream_run(agent, goal, history, req.runId, ctx,
                                  route="engine_a", domain=domain)

    return StreamingResponse(_guard(gen), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, req: ResumeRequest) -> StreamingResponse:
    ctx = RunContext(
        run_id=run_id,
        approval_token=req.approvalToken,
        params_canonical=req.paramsCanonical.encode("utf-8") if req.paramsCanonical else None,
    )

    # ── 복합 실행이었나 — 계획 테이블이 기억한다 ──────────
    plan = await composite.load_plan(run_id)
    if plan and plan["current"] < len(plan["subtasks"]):
        ctx.conversation_id = plan.get("conversationId")   # 요약 훅용 — 단독 경로의
        ctx.goal = plan.get("goal")                        # metadata 복원과 같은 역할
        k = plan["current"]
        st = plan["subtasks"][k]
        agent = await agent_for_domain(st["domain"])
        thread = composite.sub_thread(run_id, k)
        snapshot = await agent.aget_state({"configurable": {"thread_id": thread}})
        command = _validated_command(req, snapshot, run_id)
        gen = composite.stream_composite(run_id, ctx, plan, resume_command=command)
        return StreamingResponse(_guard(gen), media_type="text/event-stream",
                                 headers=_SSE_HEADERS)

    # ── 단독 실행 — 체크포인트 metadata 로 복원 ────────────
    saver = await get_checkpointer()
    tup = await saver.aget_tuple({"configurable": {"thread_id": run_id}})
    if tup is None:
        # 규격 예시 그대로 {"detail": "run not found"} — BE 가 AGENT_016 으로 번역한다
        raise HTTPException(404, detail="run not found")
    meta = tup.metadata or {}
    route = meta.get("route", "engine_a")
    domain = meta.get("domain", "meeting")
    ctx.conversation_id = meta.get("conversationId")
    ctx.goal = meta.get("goal")

    if route == "simple_query":
        agent = await simple_query.get_agent()
    elif route == "engine_b":            # replan 에이전트 — domain=="replan" 고정
        from app.engine_b import replan_agent
        agent = await replan_agent.get_agent()
    else:
        agent = await agent_for_domain(domain)
    snapshot = await agent.aget_state({"configurable": {"thread_id": run_id}})
    command = _validated_command(req, snapshot, run_id)
    gen = hitl.stream_command(agent, command, run_id, ctx, route=route, domain=domain)
    return StreamingResponse(_guard(gen), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


# ── 내부 ───────────────────────────────────────────────────
def _validated_command(req: ResumeRequest, snapshot, run_id: str):
    """대기 상태(approval/question)와 재개 요청이 맞는지 검증하고 Command 를 만든다."""
    kind = _pending_kind(snapshot)
    if kind is None:
        raise HTTPException(400, detail="no pending interrupt (run already finished)")

    if kind == "question":
        # 규격: selectedIds 와 text 둘 다 비면 차단 (BE 도 REQUEST_001 로 막지만 이중 방어)
        if not req.selectedIds and not req.text:
            raise HTTPException(400, detail="selectedIds and text both empty")
        # 선택지가 우선, text 는 보조 설명 (규격). 에이전트가 읽을 문장으로 조립
        parts = []
        if req.selectedIds:
            parts.append(f"선택한 항목 id: {', '.join(req.selectedIds)}")
        if req.text:
            parts.append(f"입력: {req.text}")
        return hitl.build_resume_command("question", answer=" / ".join(parts))

    if req.decision not in ("APPROVED", "REJECTED", "ALTERNATIVE"):
        raise HTTPException(400, detail="decision must be APPROVED|REJECTED|ALTERNATIVE")
    pending = _pending_tool_call_ids(snapshot)
    if req.toolCallId not in pending:
        raise HTTPException(400, detail="toolCallId mismatch")     # 규격 예시 문자열
    # LLM 이 드물게 한 턴에 쓰기 도구를 여러 번 부르면 대기 요청도 여러 개다.
    # 미들웨어는 요청 수만큼 decision 을 요구하므로 같은 결정을 복제해 채운다.
    return hitl.build_resume_command("approval", decision=req.decision,
                                     reason=req.reason,
                                     alternative_id=req.alternativeId,
                                     n_requests=_pending_request_count(snapshot))


async def _engine_b_stream(goal: str, run_id: str, history: list[dict] | None = None):
    """engine_b 직행 어댑터 — 분석 엔진의 중립 dict 를 v2 이벤트로 번역한다.

    엔진 B 계약(runner.run_engine_b): progress 여러 번 → result 1회.
    progress → step, result.answer → done. 실패는 _guard 가 error 로 마감.

    history: 다른 라우트(simple_query/engine_a/replan)는 전부 hitl.stream_run()에
    history를 실어 직전 대화를 이어보는데, 이 순수 분석 경로만 여태 안 넘겼다 —
    "아까 그 분석 이어서 봐줘" 같은 요청이 매번 백지로 시작되던 걸 여기서 고쳤다
    (실제 사용은 runner.run_engine_b → AnalysisRequest.chat_history).
    """
    from app.engine_b.runner import run_engine_b

    answer = ""
    async for ev in run_engine_b(goal, run_id, history=history):
        if ev["type"] == "progress":
            yield sse.step(str(ev["text"])[:100])
        elif ev["type"] == "result":
            answer = ev["answer"]
    yield sse.sse_event("done", {"answer": answer or "분석 결과를 만들지 못했습니다.",
                                 "action": None})


async def _guard(gen):
    """스트림이 done/approval_request 없이 죽으면 BE 가 AGENT_017 로 판정한다.
    그래서 무슨 예외가 나든 마지막에 error 이벤트 하나는 반드시 내보낸다."""
    try:
        async for event in gen:
            yield event
    except Exception as exc:                       # noqa: BLE001 — 최후 방어선
        yield sse.error(f"작업 중 오류가 발생했습니다: {type(exc).__name__}: {exc}")


def _pending_request_count(snapshot) -> int:
    """대기 중인 미들웨어 승인 요청 수 (보통 1, LLM 이 병렬 쓰기를 하면 2+)."""
    for task in getattr(snapshot, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            v = intr.value
            if isinstance(v, dict):
                return max(1, len(v.get("action_requests") or []))
    return 1


def _pending_kind(snapshot) -> str | None:
    """멈춰 있는 interrupt 의 종류. question | approval | None(대기 없음).

    ask_user 는 payload 에 kind="question" 표식을 심는다. 그 외의 interrupt 는
    전부 미들웨어의 승인 대기다.
    """
    for task in getattr(snapshot, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            v = intr.value
            if isinstance(v, dict) and v.get("kind") == "question":
                return "question"
            return "approval"
    return None


def _pending_tool_call_ids(snapshot) -> set[str]:
    """멈춘 시점의 마지막 AI 메시지에서 대기 중인 tool_call id 들을 꺼낸다."""
    messages = snapshot.values.get("messages", [])
    for msg in reversed(messages):
        calls = getattr(msg, "tool_calls", None)
        if calls:
            return {tc["id"] for tc in calls}
    return set()
