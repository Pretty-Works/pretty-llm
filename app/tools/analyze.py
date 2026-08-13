"""
analyze_impact — 엔진 A 공용 심층 분석 도구 (엔진 B 를 도구로 감싼 것)

"엔진 A 가 일하다가 깊은 분석이 필요해지면 엔진 B 를 부른다"(8/4 합의)의 구현.
모든 도메인 에이전트에 기본 장착된다. 조회 성격이라 승인 게이트는 없다.

실행 중 진행상황 중계: 엔진 B 는 수십 초 걸릴 수 있는데 도구 실행 중에는
step 이 안 나가 90초 무이벤트 차단에 걸릴 수 있다. 그래서 엔진 B 의
progress 를 runtime.stream_writer 로 밀어 넣고, hitl._drive 가
stream_mode="custom" 으로 받아 step 이벤트로 방출한다.

★ 8/12 추가 — 엔진 A 가 이번 turn에서 이미 조회한 사실(RunContext.known_facts,
  leave_balance/schedule_list/task_list 등이 채워둔다)을 질문에 코드로
  그대로 이어붙인다. 이전에는 "겹치는 일정을 질문에 그대로 담아라"를
  프롬프트로만 지시했는데, LLM 이 요약하다 정확한 값(며칠 남았는지, 정확히
  어떤 일정인지)을 놓치거나 다르게 쓸 수 있었다 — 엔진 B 는 원본 텍스트를
  그대로 받는다. 엔진 B 는 여전히 자기 데이터도 따로 조회한다(중복은 나지만
  최신성 보장); 이건 "새로 조회 안 함"이 아니라 "LLM 요약에만 기대지 않고
  원본 사실도 같이 준다"는 보강이다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.engine_b.runner import run_engine_b
from app.tools.registry import RunContext


def _with_known_facts(question: str, known_facts: dict[str, str]) -> str:
    if not known_facts:
        return question
    block = "\n".join(f"- {v}" for v in known_facts.values())
    return f"{question}\n\n[이번 대화에서 이미 확인된 사실 — 그대로 신뢰할 것]\n{block}"


@tool
async def analyze_impact(question: str, runtime: ToolRuntime[RunContext]) -> str:
    """지금 하려는 작업이 일정·인력·예산에 영향을 줄 수 있어 깊은 분석이 필요할 때,
    또는 사용자가 위험·영향에 대한 판단을 요청할 때 호출한다.

    예: 연차가 마감에 위험한지, 회의 참석자 부재가 일정에 주는 영향,
        예산 소진 속도가 괜찮은지.
    시간이 걸리는 무거운 분석이므로 단순 조회로 답할 수 있으면 부르지 마라.

    question: 분석할 질문을 완결된 한 문장으로
              (예: "이하늘이 8/11 하루 빠지면 베타 오픈 일정에 위험이 있는가?")
              이미 조회한 잔여·일정·할일 같은 세부 수치는 다시 요약해 넣지
              않아도 된다 — known_facts 에 있으면 코드가 그대로 덧붙인다.
    """
    ctx = runtime.context
    answer = "분석 결과를 받지 못했습니다."
    full_question = _with_known_facts(question, ctx.known_facts)

    async for ev in run_engine_b(full_question, ctx.run_id):
        # ★ 계약(모듈 docstring)은 dict 만 오게 돼 있지만, 실제로 SSE 문자열이
        #   섞여 나온 사고가 있었다(engine_b.runner._run_baseline 이 폴백 경로에서
        #   yield _sse(...) 로 문자열을 흘림 → ev["type"] 이 TypeError 로 터지고
        #   런 전체가 AGENT_007 로 죽음). 남의 모듈 회귀 하나가 사용자의 요청을
        #   통째로 날리지 않도록, 계약을 어긴 이벤트는 조용히 건너뛴다.
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "progress" and runtime.stream_writer:
            runtime.stream_writer({"text": ev.get("text", "")})  # → _drive 가 step 으로 방출
        elif ev.get("type") == "result":
            answer = ev.get("answer") or answer

    return f"[심층 분석 결과]\n{answer}"
