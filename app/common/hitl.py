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

from collections.abc import AsyncIterator

from app.common import sse
from app.tools.registry import WRITE_TOOLS, RunContext, build_request, catalog_name

# 도구 이름 → 사용자에게 보여줄 진행 문구 (step.text 는 FE 에 그대로 노출됨)
_STEP_TEXT = {
    "project_search": "프로젝트를 찾는 중...",
    "project_members": "참석자 정보를 확인하는 중...",
    "meeting_list": "회의록 목록을 확인하는 중...",
    "meeting_create": "회의록을 저장하는 중...",
}

# approvalId·questionId 는 우리가 만들지 않는다 — BE 가 주입한다 (규격 명시:
# "questionId: BE가 주입한다. LLM은 보내지 않는다". approval 도 동일 구조)


def build_resume_command(kind: str, decision: str | None = None,
                         reason: str | None = None,
                         answer: str | None = None,
                         alternative_id: str | None = None,
                         n_requests: int = 1) -> Command:
    """재개 입력을 Command 로 조립한다. kind: approval | question

    형식이 둘인 이유: 미들웨어 interrupt(승인)는 decisions 목록 봉투를
    기대하지만, ask_user 안의 interrupt()(질문)는 값을 그대로 돌려받는다.

    n_requests: 대기 중인 승인 요청 수. 미들웨어는 요청 수만큼 decision 을
    요구하므로 (LLM 이 병렬 쓰기를 한 경우) 같은 결정을 복제한다.
    """
    if kind == "question":
        return Command(resume=answer)

    if decision == "APPROVED":
        d: dict = {"type": "approve"}
    elif decision == "ALTERNATIVE":
        # 미들웨어 어휘에 ALTERNATIVE 가 없으므로 reject + 지시문으로 전달.
        # 규격: 이때 토큰이 없으므로 그 도구를 실행하면 안 된다 (AGENT_014).
        d = {"type": "reject",
             "message": (f"사용자가 저장 대신 대안 '{alternative_id}' 를 선택했습니다. "
                         "이 도구를 다시 부르지 말고, 그 대안의 방식으로 마무리하세요.")}
    else:                            # REJECTED
        d = {"type": "reject"}
        if reason:
            d["message"] = reason    # 에이전트가 사유를 읽고 대안을 답한다
    return Command(resume={"decisions": [d] * max(1, n_requests)})


async def stream_run(agent, goal: str, history: list[dict], run_id: str,
                     ctx: RunContext, route: str = "engine_a",
                     domain: str = "meeting") -> AsyncIterator[str]:
    """Run 시작 세그먼트: goal 을 실행하고 이벤트를 흘려보낸다."""
    messages = [
        {"role": "user" if m["role"] == "USER" else "assistant", "content": m["content"]}
        for m in history
    ] + [{"role": "user", "content": goal}]

    async for event in _drive(agent, {"messages": messages}, run_id, ctx, route, domain):
        yield event


async def stream_command(agent, command: Command, run_id: str, ctx: RunContext,
                         route: str = "engine_a",
                         domain: str = "meeting") -> AsyncIterator[str]:
    """재개 세그먼트: build_resume_command 로 만든 입력으로 멈춘 지점부터 실행."""
    async for event in _drive(agent, command, run_id, ctx, route, domain):
        yield event


async def _drive(agent, agent_input, run_id: str, ctx: RunContext,
                 route: str = "engine_a", domain: str = "meeting",
                 emit_done: bool = True,
                 result_sink: dict | None = None) -> AsyncIterator[str]:
    """세그먼트의 공통 몸통. 에이전트를 astream 으로 돌리며 이벤트로 변환한다.

    route·domain 을 config metadata 에 실어 체크포인트에 함께 저장한다 —
    /resume 은 goal 이 없어 재분류가 불가능하므로, 어느 에이전트로 돌아갈지를
    체크포인트가 기억해야 한다.

    emit_done=False · result_sink: 복합 실행기(composite)용 — 중간 작업의
    완료는 done 이벤트가 아니라 sink 로 알리고, done 은 마지막에 한 번만.
    """
    config = {"configurable": {"thread_id": run_id},
              "metadata": {"route": route, "domain": domain,
                           "conversationId": ctx.conversation_id,
                           "goal": (ctx.goal or "")[:500]}}
    tool_call_ids: dict[str, str] = {}      # 도구 이름 → tool_call id (interrupt 대조용)
    seen_calls: set[str] = set()            # 미들웨어가 같은 메시지를 재방출하므로 중복 제거
    final_text = ""

    # "custom" 을 같이 구독하는 이유: 오래 걸리는 도구(analyze_impact 의 엔진 B)가
    # 실행 도중 runtime.stream_writer 로 밀어 넣는 진행상황을 step 으로 중계해야
    # 90초 무이벤트 차단(규격)에 안 걸린다.
    async for mode, chunk in agent.astream(agent_input, config=config, context=ctx,
                                           stream_mode=["updates", "custom"]):
        if mode == "custom":
            text = chunk.get("text") if isinstance(chunk, dict) else str(chunk)
            if text:
                yield sse.step(str(text)[:100])
            continue

        update = chunk
        # ① 멈춤 — 무엇이 멈췄는지에 따라 이벤트가 갈린다. 스트림은 둘 다 닫는다.
        #    · 미들웨어가 쓰기 도구를 가로챔  → approval_request
        #    · ask_user 가 스스로 interrupt() → question
        #    멈춘 위치는 checkpointer 가 이미 저장했다 (runId 로 복원 가능).
        if "__interrupt__" in update:
            value = update["__interrupt__"][0].value
            if isinstance(value, dict) and value.get("kind") == "question":
                payload = {k: v for k, v in value.items() if k != "kind"}
                yield sse.sse_event("question", payload)
            else:
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

    # ④ 완주 — 단독 실행이면 done 을 내보내고, 복합 실행의 중간 작업이면
    #    sink 로 결과만 전달한다 (done 은 복합 실행기가 마지막에 한 번).
    #    action 은 도구가 RunContext 에 기록해 둔 것 (meeting_create·navigate·fill_form).
    if result_sink is not None:
        result_sink["answer"] = final_text
        result_sink["action"] = ctx.action
        result_sink["completed"] = True
    if emit_done:
        yield sse.sse_event("done", {"answer": final_text, "action": ctx.action})
        # 대화 요약 카드 갱신 — 발사 후 망각 (응답 지연 0, 실패는 로그만)
        from app.common.background import fire
        from app.memory.summarize import summarize_run
        fire(summarize_run(ctx.run_id, ctx.conversation_id,
                           ctx.goal or "", final_text))


def _approval_payload(interrupts, tool_call_ids: dict[str, str]) -> dict:
    """middleware 의 __interrupt__ → 규격 approval_request 페이로드."""
    value = interrupts[0].value
    req = (value.get("action_requests") or [{}])[0] if isinstance(value, dict) else value[0]
    tool_name = req.get("action") or req.get("name", "")
    args = req.get("args", {})

    # ★ 실행 경로(meeting_tool)와 같은 build_request 를 쓴다.
    #   여기서 방출한 params 와 실행 시 보낼 바디가 항상 같아야 한다 (AGENT_015).
    _method, _path, params = build_request(tool_name, args)

    # summary: 미들웨어 description(요청별 → 전체 순) → 없으면 기본 문구. 60자 제한
    desc = req.get("description") or (value.get("description") if isinstance(value, dict) else "")
    summary = (str(desc).strip() or f"{catalog_name(tool_name)} 실행 승인 요청")[:60]

    payload = {
        "toolCallId": tool_call_ids.get(tool_name, ""),
        "tool": catalog_name(tool_name),
        "access": "WRITE",               # 조회는 interrupt 대상이 아니므로 항상 WRITE
        "summary": summary,
        "previewText": "\n".join(f"· {k}: {v}" for k, v in params.items() if v is not None),
        "params": params,
    }
    # 승인/거절 외의 제3 선택지 (규격: 선택 필드, {id,label}. "ALWAYS" id 는 BE 예약)
    alternatives = WRITE_TOOLS.get(tool_name, {}).get("alternatives")
    if alternatives:
        payload["alternatives"] = alternatives
    return payload
