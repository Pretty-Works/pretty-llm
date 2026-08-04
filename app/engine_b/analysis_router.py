"""
Engine B 진입점 (지금은 얇은 버전)

vacation처럼 Engine A를 거치지 않고, 사용자가 '분석해줘/재계획해줘'로
직접 부르는 경로. 여기가 Engine B의 문이다.

실제(담당자 2): 여기서 LangGraph 그래프를 돌린다.
   Analysis Router(domain·focus·mode) → Context Builder → Worker 병렬
   → Validator → Synthesis → (재계획이면) Tradeoff
지금은 walking skeleton이라 워커 호출(mock)까지만 얇게 관통한다.
"""

from __future__ import annotations

from app.engine_a import engine_b_client   # mock 어댑터 (실제 완성되면 내부 그래프로)
from app.schemas.request import AgentRequest
from app.schemas.state import Domain


def run(domain: Domain, req: AgentRequest) -> dict:
    """도메인 분석을 실행하고 결과를 모아 반환한다."""
    worker_outputs = engine_b_client.analyze(domain, req)   # 워커 병렬 (mock)
    # TODO(담당자 2): Validator → Synthesis 로 통합
    return {
        "domain": domain.value,
        "worker_outputs": [w.model_dump() for w in worker_outputs],
        # 실제로는 synthesis 결과(통합 추천)가 여기 들어간다
    }
