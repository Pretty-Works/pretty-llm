"""
HITL 게이트 — LangGraph interrupt/resume 방식 (교재 방식)

수동 dict 방식에서 교체됨. 이제 상태 저장은 checkpointer가, 중단은
HumanInTheLoopMiddleware가 담당한다. 여기는 그걸 감싸는 얇은 헬퍼.

흐름:
  start(agent, message, thread_id, auth) → invoke, __interrupt__ 있으면 승인요청 반환
  resume(agent, action, thread_id, reason)→ Command(resume)로 재개

thread_id 로 "제안 → 승인" 두 HTTP 요청을 이어붙인다. (suggestion_id 역할)

★ 요청자 신원은 프롬프트가 아니라 context 로 넘긴다.
  프롬프트에 넣으면 LLM 이 그 값을 도구 인자로 다시 써넣게 되어
  사용자가 문장으로 다른 id 를 주장하면 그대로 통과한다.
"""

from __future__ import annotations

from langgraph.types import Command

from app.schemas.state import AuthContext


def start(agent, message: str, thread_id: str, auth: AuthContext) -> dict:
    """에이전트 실행. 승인이 필요하면 interrupt 정보를 반환한다."""
    config = {"configurable": {"thread_id": thread_id}}
    result = agent.invoke(
        {"messages": [{"role": "user", "content": message}]},
        config=config,
        context=auth,          # 도구가 runtime.context 로 읽는다 (LLM 은 못 봄)
    )
    return _shape(result, thread_id)


# API 의 action → 미들웨어가 아는 decision type.
#   미들웨어 어휘는 approve | reject | edit 뿐이다.
#   "replan"(사유 반영 재제안)은 미들웨어 어휘가 아니므로 reject + 사유로 넘긴다.
#   그대로 보내면 미들웨어가 모르는 값이라 재개 시 실패한다.
_DECISION_TYPE = {
    "approve": "approve",
    "reject": "reject",
    "replan": "reject",
}


def resume(agent, action: str, thread_id: str, reason: str | None = None) -> dict:
    """사용자 결정으로 중단된 실행을 재개한다. action: approve | reject | replan."""
    dtype = _DECISION_TYPE.get(action)
    if dtype is None:
        raise ValueError(f"지원하지 않는 결정: {action} (approve|reject|replan)")

    decision: dict = {"type": dtype}
    if dtype == "reject" and reason:
        # 교재 방식: reject 에 사유를 실어 보내면 에이전트가 그걸 읽고 재제안한다.
        # (PRD 설계원칙 "거절 시 사유 반영해 재제안")
        decision["message"] = reason

    config = {"configurable": {"thread_id": thread_id}}
    result = agent.invoke(Command(resume={"decisions": [decision]}), config=config)
    return _shape(result, thread_id)


def _shape(result: dict, thread_id: str) -> dict:
    """invoke 결과를 응답 형태로 정리. interrupt면 승인요청, 아니면 최종 답."""
    if "__interrupt__" in result:
        interrupt = result["__interrupt__"][0].value
        return {
            "needs_approval": True,
            "thread_id": thread_id,          # 승인 시 이 값으로 resume 호출
            "pending": interrupt.get("action_requests", []),
        }
    return {
        "needs_approval": False,
        "answer": result["messages"][-1].content,
    }


# ═══════════════════════════════════════════════════════════
#  v2 스트리밍 계층 — 에이전트 실행을 SSE 이벤트로 변환
#
#  위의 start/resume(동기, vacation 원형용)과 역할은 같지만,
#  결과를 dict 로 돌려주는 대신 이벤트를 흘려보낸다는 점이 다르다.
#
#  이벤트 변환 규칙:
#    모델이 도구를 고름   → step        ("프로젝트를 찾는 중...")
#    쓰기 직전 interrupt  → approval_request  + 스트림 끝 (체크포인트에 보존)
#    끝까지 완주          → done
#  예외 처리는 여기서 하지 않는다 — api 층이 감싸서 error 이벤트를 보장한다.
# ═══════════════════════════════════════════════════════════

import itertools
from collections.abc import AsyncIterator

from app.common import sse
from app.tools.registry import RunContext, build_request, catalog_name

# 도구 이름 → 사용자에게 보여줄 진행 문구 (step.text 는 FE 에 그대로 노출됨)
_STEP_TEXT = {
    "project_search": "프로젝트를 찾는 중...",
    "project_members": "참석자 정보를 확인하는 중...",
    "meeting_list": "회의록 목록을 확인하는 중...",
    "meeting_create": "회의록을 저장하는 중...",
}

# approvalId 는 우리가 발급하는 일련번호. (BE 확인사항: BE 가 자체 키로 다시
# 붙인다면 이 값은 참조용이 된다 — 규격 예시가 int 라 int 로 맞춰둠)
_approval_seq = itertools.count(1)


async def stream_run(agent, goal: str, history: list[dict],
                     run_id: str, ctx: RunContext) -> AsyncIterator[str]:
    """Run 시작 세그먼트: goal 을 실행하고 이벤트를 흘려보낸다."""
    messages = [
        {"role": "user" if m["role"] == "USER" else "assistant", "content": m["content"]}
        for m in history
    ] + [{"role": "user", "content": goal}]

    async for event in _drive(agent, {"messages": messages}, run_id, ctx):
        yield event


async def stream_resume(agent, decision: str, run_id: str, ctx: RunContext,
                        reason: str | None = None) -> AsyncIterator[str]:
    """재개 세그먼트: 멈춘 지점부터 다시 실행. decision: APPROVED | REJECTED."""
    d: dict = {"type": "approve" if decision == "APPROVED" else "reject"}
    if d["type"] == "reject" and reason:
        d["message"] = reason        # 에이전트가 사유를 읽고 대안을 답한다

    command = Command(resume={"decisions": [d]})
    async for event in _drive(agent, command, run_id, ctx):
        yield event


async def _drive(agent, agent_input, run_id: str, ctx: RunContext) -> AsyncIterator[str]:
    """두 세그먼트의 공통 몸통. 에이전트를 astream 으로 돌리며 이벤트로 변환한다."""
    config = {"configurable": {"thread_id": run_id}}
    tool_call_ids: dict[str, str] = {}      # 도구 이름 → tool_call id (interrupt 대조용)
    seen_calls: set[str] = set()            # 미들웨어가 같은 메시지를 재방출하므로 중복 제거
    final_text = ""

    async for update in agent.astream(agent_input, config=config,
                                      context=ctx, stream_mode="updates"):
        # ① 멈춤 — approval_request 를 내보내고 스트림을 닫는다.
        #    멈춘 위치는 checkpointer 가 이미 저장했다 (runId 로 복원 가능).
        if "__interrupt__" in update:
            payload = _approval_payload(update["__interrupt__"], tool_call_ids)
            yield sse.sse_event("approval_request", payload)
            return

        for node_output in update.values():
            for msg in (node_output or {}).get("messages", []):
                # ② 모델이 도구 호출을 결정 → step
                for tc in getattr(msg, "tool_calls", None) or []:
                    if tc["id"] in seen_calls:
                        continue
                    seen_calls.add(tc["id"])
                    tool_call_ids[tc["name"]] = tc["id"]
                    yield sse.step(_STEP_TEXT.get(tc["name"], f"{tc['name']} 실행 중..."))
                # ③ 최종 답 후보 (도구 호출 없는 AI 텍스트)
                if getattr(msg, "type", "") == "ai" and not getattr(msg, "tool_calls", None):
                    final_text = msg.text if isinstance(msg.text, str) else msg.content

    # ④ 완주 — done 은 마지막 1회. action(NAVIGATE 등)은 다음 단계에서 붙인다.
    yield sse.sse_event("done", {"answer": final_text, "action": None})


def _approval_payload(interrupts, tool_call_ids: dict[str, str]) -> dict:
    """middleware 의 __interrupt__ → 규격 approval_request 페이로드."""
    value = interrupts[0].value
    req = (value.get("action_requests") or [{}])[0] if isinstance(value, dict) else value[0]
    tool_name = req.get("action") or req.get("name", "")
    args = req.get("args", {})

    # ★ 실행 경로(meeting_tool)와 같은 build_request 를 쓴다.
    #   여기서 방출한 params 와 실행 시 보낼 바디가 항상 같아야 한다 (AGENT_015).
    _method, _path, params = build_request(tool_name, args)

    # summary: 미들웨어 description(요청별 → 전체 순) → 없으면 기본 문구
    desc = req.get("description") or (value.get("description") if isinstance(value, dict) else "")
    summary = str(desc).strip() or f"{catalog_name(tool_name)} 실행 승인 요청"

    return {
        "approvalId": next(_approval_seq),
        "toolCallId": tool_call_ids.get(tool_name, ""),
        "tool": catalog_name(tool_name),
        "access": "WRITE",               # 조회는 interrupt 대상이 아니므로 항상 WRITE
        "summary": summary,
        "previewText": "\n".join(f"· {k}: {v}" for k, v in params.items() if v is not None),
        "params": params,
    }
