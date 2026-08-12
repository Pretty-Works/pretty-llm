"""
연차 승인 에이전트 — create_agent + HumanInTheLoopMiddleware (교재 방식)

HITL (교재 02_Agent/06_Middleware/02-HITL-project):
  - 되돌리기 어려운 행동(연차 실제 승인)을 @tool 로 만든다.
  - HumanInTheLoopMiddleware(interrupt_on=...)가 그 도구 호출을 가로채
    __interrupt__ 를 발생시킨다 → 사람 승인 후 Command(resume=...)로 재개.
  - checkpointer + thread_id 로 중단/재개 상태를 유지한다.

권한 (교재 02_Agent/05_Agent_Development/05-Runtime-Context):
  - 요청자 user_id 를 프롬프트에 넣지 않고 context_schema(AuthContext)로 주입한다.
    프롬프트에 넣으면 LLM 이 읽어 도구 인자로 다시 써넣는 구조가 되어,
    사용자가 "나는 99번이야" 라고 쓰면 그대로 통과한다(재현 확인됨).
    runtime.context 는 LLM 이 볼 수 없으므로 인젝션이 통하지 않는다.

이 방식이 하네스 배점("미들웨어 + LangGraph")에 직접 대응한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.chat_models import init_chat_model
from langchain.tools import ToolRuntime, tool
from langgraph.checkpoint.sqlite import SqliteSaver

from app.config import settings
from app.engine_a import engine_b_client
from app.schemas.state import AuthContext, Domain


# ── 도구 정의 ──────────────────────────────────────────────
#   ★ 요청자 user_id 를 인자로 받지 않는다.
#     "누가 요청했나"(권한 주체)는 runtime.context 에서 서버가 아는 값을 읽고,
#     "누구의 연차인가"(대상)는 사용자 문장에서 LLM 이 뽑는다.
#     대상에 대한 권한 검증은 백엔드가 한다 (PM 이 그 사원을 승인할 수 있는지).
@tool
def check_vacation_impact(target_employee: str, period: str,
                          runtime: ToolRuntime[AuthContext]) -> str:
    """연차가 프로젝트에 미치는 영향을 분석한다 (읽기 전용 = 승인 불필요).

    target_employee: 연차 대상 사원 (이름 또는 사번)
    period: 연차 기간 (예: "2026-03-02 ~ 2026-03-06")

    내부에서 Engine B(mock)를 불러 Risk‖Workload 근거를 가져온다.
    """
    # AgentRequest 형태가 아니라 tool 인자를 받으므로 얇은 어댑터로 호출
    from app.schemas.request import AgentRequest
    req = AgentRequest(
        message=f"{target_employee} {period}",
        user_id=runtime.context.user_id,      # 요청자 = 조회 권한 주체
        domain_hint=Domain.vacation,
    )
    notes = engine_b_client.analyze(Domain.vacation, req)
    lines = [f"- [{n.dimension}] {n.reasoning} (conf {n.confidence})" for n in notes]
    return f"{target_employee} {period} 연차 영향 분석:\n" + "\n".join(lines)


@tool
def approve_vacation(target_employee: str, period: str,
                     runtime: ToolRuntime[AuthContext]) -> str:
    """연차를 실제로 승인한다 (되돌리기 어려움 = 사람 승인 필요).

    target_employee: 연차 대상 사원 (이름 또는 사번)
    period: 연차 기간 (예: "2026-03-02 ~ 2026-03-06")

    ★ 이 도구가 HumanInTheLoopMiddleware의 interrupt 대상이다.
    """
    # TODO: 실제 승인 반영은 백엔드 호출 (지금은 관통 확인용 텍스트)
    #       요청자(runtime.context.user_id)를 헤더에 실어 보내면 백엔드가 권한 검증
    return (f"요청자 {runtime.context.user_id} 승인 → "
            f"{target_employee} 연차({period}) 승인 완료")


# ── checkpointer ───────────────────────────────────────────
def _build_checkpointer() -> SqliteSaver:
    """승인 대기 상태를 파일에 보관한다.

    check_same_thread=False: FastAPI 가 동기 핸들러를 스레드풀에서 돌리므로
    커넥션을 만든 스레드와 사용하는 스레드가 다를 수 있다.
    """
    path = Path(settings.checkpoint_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()          # 테이블 생성 (최초 1회, 이후 no-op)
    return saver


# ── 에이전트 조립 ──────────────────────────────────────────
def build_vacation_agent():
    """연차 승인 에이전트를 만든다. approve_vacation 호출 시 사람 승인을 받는다."""
    # temperature 를 안 넘기면 provider 기본값으로 돌아 같은 질문에도 도구 선택이
    # 흔들린다 (실측: 동일 입력 5회에 응답 2가지). 라우팅·도구 선택은 결정적이어야 한다.
    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider,
                                temperature=settings.llm_temperature)
    return create_agent(
        model,
        tools=[check_vacation_impact, approve_vacation],
        context_schema=AuthContext,          # runtime.context 주입 활성화
        middleware=[
            HumanInTheLoopMiddleware(
                interrupt_on={
                    # allowed_decisions 를 명시한다. True 로 두면 edit 까지 허용되는데
                    # DecisionRequest 가 edit(인자 수정)을 받지 못해 규격이 어긋난다.
                    "approve_vacation": {"allowed_decisions": ["approve", "reject"]},
                    "check_vacation_impact": False,   # 읽기 = 자동 실행
                },
                description_prefix="연차 승인",
            )
        ],
        checkpointer=_build_checkpointer(),
    )


# 앱 전역 단일 인스턴스 (thread_id로 세션 구분)
_agent = None


def get_agent():
    """싱글톤 + 지연 생성.

    왜 함수로 감쌌나: create_agent는 내부에서 LLM 모델을 만든다(API 키 사용).
    모듈 최상단에 두면 import 되는 순간 모델이 생성돼, 키 없이 문법 검증·테스트가
    안 되고 서버 기동 시 불필요하게 미리 만들어진다. 그래서 '처음 실제로 쓸 때
    한 번만' 만들고 이후 재사용한다.
    (더 정석은 FastAPI lifespan/dependency 주입 — 나중에 옮길 수 있음)
    """
    global _agent
    if _agent is None:
        _agent = build_vacation_agent()
    return _agent
