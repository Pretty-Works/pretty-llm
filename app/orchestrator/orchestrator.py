"""
Orchestrator — 요청을 받아 어느 도메인 핸들러로 보낼지 결정

★ 설계원칙 2 — 분기를 if문이 아니라 매핑(dict)으로
   도메인이 늘어날 때 이 함수를 뜯어고치지 않는다. HANDLERS 표에 한 줄 등록만.
   (Tool Dispatcher가 '고정 매핑'인 것과 같은 원리)

지금은 vacation 하나만 등록. 3단계에서 meeting/project 등을 표에 추가한다.
LLM 기반 domain 분류도 3단계에서 붙인다 (지금은 domain_hint 사용).
"""

from __future__ import annotations

from app.engine_b import analysis_router
from app.schemas.request import AgentRequest
from app.schemas.state import Domain

# 참고: vacation(승인 업무)은 create_agent + HITL 미들웨어로 처리되므로
#       api/vacation.py 에서 에이전트를 직접 호출한다 (orchestrator 경유 안 함).
#       orchestrator는 '조회성 분석'처럼 즉시 결과를 내는 경로를 담당한다.


def _handle_project(req: AgentRequest) -> dict:
    """엔진 B 직접 진입: 분석 요청 → 엔진 B 실행. 읽기 전용이라 HITL 없음."""
    return analysis_router.run(Domain.project, req)


# 도메인 → 핸들러 매핑. 새 도메인은 여기 한 줄 추가로 끝. (설계원칙 2)
HANDLERS = {
    Domain.project: _handle_project,     # 엔진 B 직접
    # Domain.meeting: _handle_meeting,
}


def handle(req: AgentRequest) -> dict:
    """진입점. 지금은 domain_hint로 분기 (LLM 분류는 3단계)."""
    domain = req.domain_hint or Domain.vacation
    handler = HANDLERS.get(domain)
    if handler is None:
        return {"error": f"no handler for {domain}"}   # 나중에 예외로 교체
    return handler(req)
