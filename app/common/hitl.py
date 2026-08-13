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

import asyncio
import time
import re
from collections.abc import AsyncIterator

from app.common import sse
from app.common.run_context import current_run_id
from app.config import get_settings
from app.tools.registry import WRITE_TOOLS, RunContext, build_request, catalog_name, is_mcp_write
from app.utils.logger import get_logger

log = get_logger("common.hitl")

# ★ 2026-08-13 추가 — 동시 사용자가 늘면서 gpt-4o-mini TPM 한도(Tier1: 200,000)를
#   넘겨 429(RateLimitError)가 나는 사례가 늘었다. Run 하나가 LangGraph 안에서
#   OpenAI 를 여러 번(도구 호출마다) 부를 수 있어 "동시 사용자 수"와 "동시 OpenAI
#   호출량"이 비례하지 않으므로, 사용자 요청 건수가 아니라 "동시에 실행 중인 Run
#   수"를 이 세마포어로 제한한다. engine_a 의 모든 도메인 에이전트(할일·일정·
#   지출·메일·재계획·회의·휴가 등)와, 그 안에서 analyze_impact 로 불리는 engine_b
#   까지 전부 결국 이 함수(_drive)를 거치므로 여기 한 곳이면 전체 Run 이 걸린다.
#   ★ 프로세스 전역(모듈 레벨) 세마포어라 인스턴스 1개 기준이다. 여러 인스턴스/
#   컨테이너로 늘어나면 프로세스별 세마포어만으론 전체 동시성을 못 막으므로,
#   그때는 Redis 등 전역 rate limiter 로 바꿔야 한다(별도 검토 필요).
_AGENT_SEMAPHORE = asyncio.Semaphore(get_settings().agent_concurrency_limit)

# ★ 8/13 추가 — COMMON_RULES 가 "작업 완료 보고:" 로 시작하지 말라고 명시적으로
#   금지했는데도(그 문구가 규칙의 "잘못" 예시 그 자체) 실사용에서 그대로 나온
#   사례가 있었다. 프롬프트만으론 한계가 있어, 최종 답변에서 코드로 한 번 더 잘라낸다.
_BANNED_PREFIX = re.compile(r"^\s*작업\s*완료\s*보고\s*[:：]\s*")


def _strip_banned_prefix(text: str) -> str:
    return _BANNED_PREFIX.sub("", text, count=1)

# 도구 이름 → 사용자에게 보여줄 진행 문구 (step.text 는 FE 에 그대로 노출됨)
#   ★ 도구를 추가하면 여기도 채운다. 빠뜨리면 "ask_user 실행 중..." 처럼 **영어
#     도구 이름이 사용자 화면에 그대로 노출된다** (실사용 피드백: "ask_user 가 뭐죠?").
#     규격: step.text 는 "사용자에게 보일 한국어 한 줄, 100자 이하".
_STEP_TEXT = {
    # 조회 — 사람
    "user_me": "오늘 날짜와 내 정보를 확인하는 중...",
    "user_search": "사원을 찾는 중...",
    # 조회 — 프로젝트
    "project_search": "프로젝트를 찾는 중...",
    "project_members": "참여자 정보를 확인하는 중...",
    "milestone_list": "마일스톤을 확인하는 중...",
    "budget_summary": "예산 현황을 확인하는 중...",
    "expense_list": "지출 내역을 확인하는 중...",
    # 조회 — 일감
    "task_list": "할 일을 확인하는 중...",
    "meeting_list": "회의록 목록을 확인하는 중...",
    "meeting_detail": "회의록 내용을 읽는 중...",
    "schedule_list": "일정을 확인하는 중...",
    "leave_balance": "연차 잔여를 확인하는 중...",
    "leave_list": "휴가 내역을 확인하는 중...",
    "task_due_within": "그 기간 마감인 할 일을 정리하는 중...",
    # 기억·문서
    "recall": "예전 기록을 찾아보는 중...",
    "doc_search": "사내 문서를 찾아보는 중...",
    # ★ 문구에 "왜 부르는지"를 담는다 — 엔진 B 는 오래 걸리는데 "분석 중"만 뜨면
    #   사용자는 멈춘 줄 안다. 판단이 애매해서 깊이 보는 중이라고 알려준다.
    "analyze_impact": "판단이 애매해 조금 더 깊이 분석하는 중...",
    "propose_replan_scenarios": "일정·인력·범위 조정안 3가지를 만드는 중...",
    # 쓰기 (승인 카드 직전)
    "meeting_create": "회의록을 저장하는 중...",
    "meeting_draft_fill": "회의록 초안을 작성하는 중...",
    "task_create": "할 일을 등록하는 중...",
    "task_toggle_status": "할 일 상태를 바꾸는 중...",
    "task_update": "할 일 내용을 수정하는 중...",
    "schedule_create": "일정을 등록하는 중...",
    "schedule_update": "일정을 변경하는 중...",
    "leave_create": "휴가를 신청하는 중...",
    "leave_update": "휴가 신청을 변경하는 중...",
    "expense_create": "지출을 등록하는 중...",
    "milestone_toggle_status": "마일스톤 상태를 바꾸는 중...",
    "replan_save": "재계획 안을 저장하는 중...",
    "replan_apply": "재계획을 반영하는 중...",
    # 상호작용
    "ask_user": "확인이 필요해 여쭤보는 중...",
    "navigate": "이동할 화면을 준비하는 중...",
    "fill_form": "화면에 채울 내용을 준비하는 중...",
}

# MCP 로 동적으로 붙는 도구는 이름이 서버에서 오므로 접두사로 받는다.
_STEP_TEXT_PREFIX = {
    "gmail_send": "메일을 보내는 중...",
    "gmail_": "메일을 확인하는 중...",
}


def _step_text(tool_name: str) -> str:
    """도구 이름 → 사용자에게 보일 한국어 한 줄.

    등록되지 않은 도구라도 **영어 이름을 노출하지 않는다** — 사용자는 우리 내부
    도구명을 알 이유가 없고, 실제로 "ask_user 가 뭐죠?" 라는 피드백을 받았다.
    """
    if tool_name in _STEP_TEXT:
        return _STEP_TEXT[tool_name]
    for prefix, text in _STEP_TEXT_PREFIX.items():
        if tool_name.startswith(prefix):
            return text
    return "작업을 진행하는 중..."

# approvalId·questionId 는 우리가 만들지 않는다 — BE 가 주입한다 (규격 명시:
# "questionId: BE가 주입한다. LLM은 보내지 않는다". approval 도 동일 구조)


def build_resume_command(kind: str, decision: str | None = None,
                         reason: str | None = None,
                         answer: str | None = None,
                         alternative_id: str | None = None,
                         n_requests: int = 1,
                         interrupt_ids: list[str] | None = None) -> Command:
    """재개 입력을 Command 로 조립한다. kind: approval | question

    형식이 둘인 이유: 미들웨어 interrupt(승인)는 decisions 목록 봉투를
    기대하지만, ask_user 안의 interrupt()(질문)는 값을 그대로 돌려받는다.

    n_requests: 대기 중인 승인 요청 수. 미들웨어는 요청 수만큼 decision 을
    요구하므로 (LLM 이 병렬 쓰기를 한 경우) 같은 결정을 복제한다.

    ★ 8/13 추가 — interrupt_ids: 대기 중인 interrupt 가 **2개 이상이면**
      langgraph 가 "어느 interrupt 에 대한 답인지" id 를 요구한다
      (RuntimeError: When there are multiple pending interrupts...).
      LLM 이 ask_user 를 병렬로 두 번 부르면 실제로 이 상태가 되고, 그때
      Command(resume=값) 만 보내면 실행이 통째로 죽는다(실측: test_action 3/3 재현).
      그래서 id 가 2개 이상 넘어오면 {id: 값} 맵으로 만들어 전부 같은 답을 준다 —
      우리 규격상 BE 는 질문 하나에 대한 답만 보내오므로, 남은 interrupt 를 그대로
      두면 재개가 영영 안 끝난다.
    """
    if kind == "question":
        if interrupt_ids and len(interrupt_ids) > 1:
            return Command(resume={iid: answer for iid in interrupt_ids})
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
    # ★ 2026-08-13 추가 — 이 Run 이 실제로 OpenAI 를 몇 번 불렀고 토큰을 얼마나
    #   썼는지. "사용자 요청 1건당 실제 호출 횟수·토큰"을 알아야 TPM 한도(429 원인)
    #   대비 실제 부담을 가늠할 수 있다 — 도구 호출이 반복되는 흐름은 겉보기
    #   요청 1건이 LLM 호출 여러 번으로 불어난다.
    llm_calls = 0
    tokens_in = tokens_out = 0

    # gmail_mcp_client._lock_run_id() 가 감싼 도구들은 RunContext(context=ctx)가
    # 아니라 이 contextvar 로 run_id 를 읽는다(engine_b/runner.py 와 동일 패턴).
    # 여기서 안 하면 gmail 도구는 항상 current_run_id.get() == None 을 보고
    # {"connected": False, "error": "no_active_run"} 만 돌려주게 된다 — 즉 도메인
    # 에이전트에 gmail 도구를 섞는 순간부터는 반드시 필요한 한 줄이다. resume
    # 세그먼트(stream_command)도 같은 _drive() 를 타므로 여기 한 곳이면 충분하다.
    current_run_id.set(run_id)

    # ★ 2026-08-13 추가 — 동시 실행 수 제한(_AGENT_SEMAPHORE, 모듈 상단 참고).
    #   대기가 있었으면(큐가 밀렸다는 뜻) 로그로 남긴다 — 평소엔 즉시 획득되므로
    #   0.05s 문턱값 이하는 로그를 안 남겨 소음을 줄인다. astream 루프 전체와
    #   summarize_run()(마지막 LLM 호출 1번)까지 세마포어를 쥔 채로 진행한다 —
    #   OpenAI 를 실제로 두드리는 구간 전부를 덮어야 의미가 있다. 중간에 interrupt
    #   로 return 하거나 클라이언트가 스트림을 끊어도(GeneratorExit) `async with`
    #   가 세마포어를 정상 반납한다.
    wait_start = time.monotonic()
    async with _AGENT_SEMAPHORE:
        waited = time.monotonic() - wait_start
        if waited > 0.05:
            log.info("[%s/%s] run_id=%s 동시 실행 한도(%d)로 %.2fs 대기 후 시작",
                     route, domain, run_id, get_settings().agent_concurrency_limit, waited)

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
                    payload = await _approval_payload(update["__interrupt__"], tool_call_ids, run_id)
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
                        yield sse.step(_step_text(tc["name"]))
                    # ②-1 토큰 사용량 — AIMessage 에 usage_metadata 가 실려 오면
                    #   (일반 응답이든 도구 호출 응답이든) 그때마다 실제 OpenAI 호출
                    #   1회로 센다. engine_b(llm_client.py) 는 이미 자체적으로 이
                    #   로그를 남기므로, 여기는 engine_a 경로(이전엔 로깅이 아예
                    #   없었다)를 채운다.
                    usage = getattr(msg, "usage_metadata", None)
                    if usage:
                        llm_calls += 1
                        c_in = int(usage.get("input_tokens") or 0)
                        c_out = int(usage.get("output_tokens") or 0)
                        tokens_in += c_in
                        tokens_out += c_out
                        log.info(
                            "[%s/%s] run_id=%s LLM 호출 #%d tokens in=%s out=%s total=%s",
                            route, domain, run_id, llm_calls, c_in, c_out, c_in + c_out,
                        )
                    # ③ 최종 답 후보 (도구 호출 없는 AI 텍스트)
                    if getattr(msg, "type", "") == "ai" and not getattr(msg, "tool_calls", None):
                        final_text = msg.text if isinstance(msg.text, str) else msg.content

        if llm_calls:
            log.info(
                "[%s/%s] run_id=%s 완료 — LLM 호출 %d회 tokens in=%s out=%s total=%s",
                route, domain, run_id, llm_calls, tokens_in, tokens_out, tokens_in + tokens_out,
            )

        # ④ 완주 — 단독 실행이면 done 을 내보내고, 복합 실행의 중간 작업이면
        #    sink 로 결과만 전달한다 (done 은 복합 실행기가 마지막에 한 번).
        #    action 은 도구가 RunContext 에 기록해 둔 것 (meeting_create·navigate·fill_form).
        if result_sink is not None:
            result_sink["answer"] = final_text
            result_sink["action"] = ctx.action
            result_sink["completed"] = True
        if emit_done:
            # ★ 8/12 변경 — 채팅 목록 제목(BE GET /agent/conversations 의 title)에
            #   쓸 값을 done 바디에 실어야 해서, 더 이상 fire()(발사 후 망각)가 아니라
            #   done 을 내보내기 "전에" await 한다. LLM 호출 1번만큼 지연이 늘지만,
            #   그래야 BE 가 title 을 그대로 저장할 수 있다(자세한 이유는
            #   app/memory/summarize.py 모듈 docstring). 실패해도 None 이라 done 자체는
            #   막지 않는다 — 이때는 title 필드를 아예 안 실어 BE 가 첫 질문으로 폴백한다.
            from app.memory.summarize import summarize_run
            title = await summarize_run(ctx.run_id, ctx.conversation_id, ctx.goal or "", final_text)
            done_payload = {"answer": final_text, "action": ctx.action}
            if title:
                done_payload["title"] = title
            yield sse.sse_event("done", done_payload)
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
                payload = await _approval_payload(update["__interrupt__"], tool_call_ids, run_id)
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
                    # 실사용 디버깅용 — BE 로그는 실패한 API 호출의 경로만 보여주고
                    # 어느 tool 이 어떤 인자로 그 호출을 만들었는지는 안 보인다.
                    # print 는 uvicorn 터미널에 남는다 (engine_b/runner.py 와 동일 패턴).
                    print(f"[tool_call] {tc['name']}({tc.get('args')})")
                    yield sse.step(_step_text(tc["name"]))
                # ③ 최종 답 후보 (도구 호출 없는 AI 텍스트)
                if getattr(msg, "type", "") == "ai" and not getattr(msg, "tool_calls", None):
                    text = msg.text if isinstance(msg.text, str) else msg.content
                    final_text = _strip_banned_prefix(text) if isinstance(text, str) else text

    # ④ 완주 — 단독 실행이면 done 을 내보내고, 복합 실행의 중간 작업이면
    #    sink 로 결과만 전달한다 (done 은 복합 실행기가 마지막에 한 번).
    #    action 은 도구가 RunContext 에 기록해 둔 것 (meeting_create·navigate·fill_form).
    if result_sink is not None:
        result_sink["answer"] = final_text
        result_sink["action"] = ctx.action
        result_sink["completed"] = True
    if emit_done:
        # ★ 8/12 변경 — 채팅 목록 제목(BE GET /agent/conversations 의 title)에
        #   쓸 값을 done 바디에 실어야 해서, 더 이상 fire()(발사 후 망각)가 아니라
        #   done 을 내보내기 "전에" await 한다. LLM 호출 1번만큼 지연이 늘지만,
        #   그래야 BE 가 title 을 그대로 저장할 수 있다(자세한 이유는
        #   app/memory/summarize.py 모듈 docstring). 실패해도 None 이라 done 자체는
        #   막지 않는다 — 이때는 title 필드를 아예 안 실어 BE 가 첫 질문으로 폴백한다.
        from app.memory.summarize import summarize_run
        title = await summarize_run(ctx.run_id, ctx.conversation_id, ctx.goal or "", final_text)
        done_payload = {"answer": final_text, "action": ctx.action}
        if title:
            done_payload["title"] = title
        yield sse.sse_event("done", done_payload)


async def _approval_payload(interrupts, tool_call_ids: dict[str, str], run_id: str) -> dict:
    """middleware 의 __interrupt__ → 규격 approval_request 페이로드.

    ★ 8/12 추가 — leave_create 는 여기서 도구별 사전 경고를 한 번 더 얹는다.
      "신청" 승인 카드가 뜨기 전에 잔여 초과·마감 겹침을 previewText 에 보여줘야
      사용자가 승인 버튼을 누르기 "전에" 알 수 있다 (leave_create 실행 시점
      경고는 이미 승인한 뒤라 너무 늦다). 다른 도구는 영향 없다.
    """
    value = interrupts[0].value
    req = (value.get("action_requests") or [{}])[0] if isinstance(value, dict) else value[0]
    tool_name = req.get("action") or req.get("name", "")
    args = req.get("args", {})

    if is_mcp_write(tool_name):
        # gmail_send_email 같은 MCP 쓰기 도구 — BE 내부 API 경로가 없어
        # build_request()를 못 쓴다(WRITE_TOOLS[tool_name] KeyError 남). 이 도구들은
        # execute_write()/AGENT_015 해시 재검증 경로를 아예 안 타므로(실행이 BE가
        # 아니라 MCP 서버로 직접 나감), 승인 카드 표시용으로 LLM이 준 args를
        # 그대로 params 로 쓰면 된다 — "방출한 params == 실행 바디"를 맞출 대상
        # 자체가 없다.
        params = args
    else:
        # ★ 실행 경로(meeting_tool)와 같은 build_request 를 쓴다.
        #   여기서 방출한 params 와 실행 시 보낼 바디가 항상 같아야 한다 (AGENT_015).
        _method, _path, params = build_request(tool_name, args)

    # summary: 규격은 "카드 제목 한 줄, 60자 이하". 미들웨어가 만드는 description 은
    #   "{prefix}\n\nTool: meeting_create\nArgs: {...}"
    # 처럼 여러 줄이라 그대로 넣으면 규격을 어기고, 앞 60자를 잘라도 사람에게 쓸모없는
    # Args 조각만 남는다(prefix+Tool 이 35자라 경계가 Args 한가운데 떨어짐 — 실측).
    # 그래서 우리가 준 첫 줄(description_prefix)만 쓰고 나머지 디버그 줄은 버린다.
    desc = req.get("description") or (value.get("description") if isinstance(value, dict) else "")
    head = next((ln.strip() for ln in str(desc or "").splitlines() if ln.strip()), "")
    summary = " ".join(head.split())[:60] or f"{catalog_name(tool_name)} 실행 승인 요청"

    preview_text = "\n".join(f"· {k}: {v}" for k, v in params.items() if v is not None)

    if tool_name == "leave_create":
        from app.tools.leave_tool import preview_leave_risks
        try:
            risk = await preview_leave_risks(
                run_id, args.get("leaveType"), args.get("startDate"), args.get("endDate"))
        except Exception:
            risk = ""
        if risk:
            preview_text = f"⚠️ {risk}\n" + preview_text

    payload = {
        "toolCallId": tool_call_ids.get(tool_name, ""),
        "tool": catalog_name(tool_name),
        "access": "WRITE",               # 조회는 interrupt 대상이 아니므로 항상 WRITE
        "summary": summary,
        "previewText": preview_text,
        "params": params,
    }
    # 승인/거절 외의 제3 선택지 (규격: 선택 필드, {id,label}. "ALWAYS" id 는 BE 예약)
    alternatives = WRITE_TOOLS.get(tool_name, {}).get("alternatives")
    if alternatives:
        payload["alternatives"] = alternatives
    return payload
