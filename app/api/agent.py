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
    toolCallId: str
    decision: str                        # APPROVED | REJECTED
    approvalToken: str | None = None     # WRITE 승인일 때만. X-Approval-Token 으로 전달됨
    reason: str | None = None            # 거절 사유 (200자 이하)
    paramsCanonical: str | None = None   # BE 가 해시한 바이트 원본 (요청해둔 확장)


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

    # ② 대기 중인 도구 호출과 toolCallId 가 맞는가 — 어긋나면 다른 승인의 토큰이다
    pending = _pending_tool_call_ids(snapshot)
    if req.toolCallId not in pending:
        raise HTTPException(400, detail={
            "errorCode": None,
            "message": f"toolCallId 불일치: {req.toolCallId} (대기 중: {sorted(pending)})",
        })

    ctx = RunContext(
        run_id=run_id,
        approval_token=req.approvalToken,
        params_canonical=req.paramsCanonical.encode("utf-8") if req.paramsCanonical else None,
    )
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


def _pending_tool_call_ids(snapshot) -> set[str]:
    """멈춘 시점의 마지막 AI 메시지에서 대기 중인 tool_call id 들을 꺼낸다."""
    messages = snapshot.values.get("messages", [])
    for msg in reversed(messages):
        calls = getattr(msg, "tool_calls", None)
        if calls:
            return {tc["id"] for tc in calls}
    return set()
