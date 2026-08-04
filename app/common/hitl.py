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
