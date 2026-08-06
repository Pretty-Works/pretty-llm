"""
analyze_impact — 엔진 A 공용 심층 분석 도구 (엔진 B 를 도구로 감싼 것)

"엔진 A 가 일하다가 깊은 분석이 필요해지면 엔진 B 를 부른다"(8/4 합의)의 구현.
모든 도메인 에이전트에 기본 장착된다. 조회 성격이라 승인 게이트는 없다.

실행 중 진행상황 중계: 엔진 B 는 수십 초 걸릴 수 있는데 도구 실행 중에는
step 이 안 나가 90초 무이벤트 차단에 걸릴 수 있다. 그래서 엔진 B 의
progress 를 runtime.stream_writer 로 밀어 넣고, hitl._drive 가
stream_mode="custom" 으로 받아 step 이벤트로 방출한다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.engine_b.runner import run_engine_b
from app.tools.registry import RunContext


@tool
async def analyze_impact(question: str, runtime: ToolRuntime[RunContext]) -> str:
    """지금 하려는 작업이 일정·인력·예산에 영향을 줄 수 있어 깊은 분석이 필요할 때,
    또는 사용자가 위험·영향에 대한 판단을 요청할 때 호출한다.

    예: 연차가 마감에 위험한지, 회의 참석자 부재가 일정에 주는 영향,
        예산 소진 속도가 괜찮은지.
    시간이 걸리는 무거운 분석이므로 단순 조회로 답할 수 있으면 부르지 마라.

    question: 분석할 질문을 완결된 한 문장으로
              (예: "이하늘이 8/11 하루 빠지면 베타 오픈 일정에 위험이 있는가?")
    """
    ctx = runtime.context
    answer = "분석 결과를 받지 못했습니다."

    async for ev in run_engine_b(question, ctx.run_id):
        if ev["type"] == "progress" and runtime.stream_writer:
            runtime.stream_writer({"text": ev["text"]})     # → _drive 가 step 으로 방출
        elif ev["type"] == "result":
            answer = ev["answer"]

    return f"[심층 분석 결과]\n{answer}"
