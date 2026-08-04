"""
에이전트 전용 API — 규격 v2 진입점 (BE ↔ FastAPI)

POST /api/agent/runs                : Run 시작. goal 실행 → SSE 스트림
POST /api/agent/runs/{runId}/resume : 승인/거절 후 재개 → SSE 스트림

두 엔드포인트 다 응답은 text/event-stream 이다. 팀원들의 /api/v1/** (REST)
와 별개 계약이라 prefix 를 공유하지 않는다.

지금은 goal 이 무엇이든 회의록 에이전트로 직결한다(관통 우선).
오케스트레이터 3분기가 준비되면 아래 get_agent() 한 줄이
  route = classify(goal, screenContext) → HANDLERS[route]
로 바뀐다. 이 파일에서 바뀌는 건 그 한 줄뿐이다.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.common import hitl, sse
from app.engine_a.meeting_agent import get_agent
from app.tools.registry import RunContext

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


class RunRequest(BaseModel):
    runId: str           # BE 가 발급. 체크포인트 thread_id 로 그대로 쓴다
    conversationId: int
    goal: str
    messages: list[MessageItem] = Field(default_factory=list)   # 최근 10건
    screenContext: ScreenContext = Field(default_factory=ScreenContext)
    requestSource: str = "WEB"
    locale: str = "ko-KR"


class ResumeRequest(BaseModel):
    """승인 재개와 질문 답변 재개를 한 몸으로 받는다.

    대기 중인 게 approval 이면 toolCallId·decision 이 필수,
    question 이면 answer 가 필수 — 검증은 엔드포인트에서 상태를 보고 한다.
    (질문 답변의 정확한 필드명은 노션 재확인 때 최종 대조 — C 단계)
    """
    # 승인 재개용
    toolCallId: str | None = None
    decision: str | None = None          # APPROVED | REJECTED
    approvalToken: str | None = None     # WRITE 승인일 때만. X-Approval-Token 으로 전달됨
    reason: str | None = None            # 거절 사유 (200자 이하)
    paramsCanonical: str | None = None   # BE 가 해시한 바이트 원본 (요청해둔 확장)
    # 질문 답변용
    questionId: int | None = None
    answer: str | None = None            # 선택지 id 또는 자유 입력 텍스트


# ── 엔드포인트 ─────────────────────────────────────────────
@router.post("/runs")
async def start_run(req: RunRequest) -> StreamingResponse:
    agent = await get_agent()
    ctx = RunContext(run_id=req.runId)
    history = [m.model_dump() for m in req.messages]

    gen = hitl.stream_run(agent, req.goal, history, req.runId, ctx)
    return StreamingResponse(_guard(gen), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, req: ResumeRequest) -> StreamingResponse:
    agent = await get_agent()

    # ① 체크포인트가 있는가 — 없으면 이어붙일 실행이 없다 (규격 AGENT_016)
    snapshot = await agent.aget_state({"configurable": {"thread_id": run_id}})
    if not snapshot.values:
        raise HTTPException(404, detail={
            "errorCode": "AGENT_016",
            "message": f"run {run_id} 의 체크포인트가 없습니다. 새 Run 으로 다시 시작하세요.",
        })

    # ② 지금 멈춰 있는 게 뭔가 — 그것이 재개 방식(승인/답변)을 결정한다
    kind = _pending_kind(snapshot)
    if kind is None:
        raise HTTPException(400, detail={
            "errorCode": None,
            "message": f"run {run_id} 은 대기 중인 중단점이 없습니다 (이미 종료됨).",
        })

    ctx = RunContext(
        run_id=run_id,
        approval_token=req.approvalToken,
        params_canonical=req.paramsCanonical.encode("utf-8") if req.paramsCanonical else None,
    )

    if kind == "question":
        if req.answer is None:
            raise HTTPException(400, detail={
                "errorCode": None,
                "message": "question 대기 중입니다 — answer 필드가 필요합니다.",
            })
        gen = hitl.stream_answer(agent, req.answer, run_id, ctx)
    else:
        if req.decision not in ("APPROVED", "REJECTED"):
            raise HTTPException(400, detail={
                "errorCode": None,
                "message": "approval 대기 중입니다 — decision(APPROVED|REJECTED)이 필요합니다.",
            })
        # toolCallId 가 대기 중인 도구 호출과 맞는가 — 어긋나면 다른 승인의 토큰이다
        pending = _pending_tool_call_ids(snapshot)
        if req.toolCallId not in pending:
            raise HTTPException(400, detail={
                "errorCode": None,
                "message": f"toolCallId 불일치: {req.toolCallId} (대기 중: {sorted(pending)})",
            })
        gen = hitl.stream_resume(agent, req.decision, run_id, ctx, reason=req.reason)

    return StreamingResponse(_guard(gen), media_type="text/event-stream",
                             headers=_SSE_HEADERS)


# ── 내부 ───────────────────────────────────────────────────
async def _guard(gen):
    """스트림이 done/approval_request 없이 죽으면 BE 가 AGENT_017 로 판정한다.
    그래서 무슨 예외가 나든 마지막에 error 이벤트 하나는 반드시 내보낸다."""
    try:
        async for event in gen:
            yield event
    except Exception as exc:                       # noqa: BLE001 — 최후 방어선
        yield sse.error(f"작업 중 오류가 발생했습니다: {type(exc).__name__}: {exc}")


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
