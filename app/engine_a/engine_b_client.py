"""
Engine B 호출 어댑터 (지금은 mock)

★ 설계원칙 1 — 인터페이스 고정, 속만 교체
   부르는 쪽은 analyze(domain, req)만 호출한다. 지금은 도메인별 가짜를 반환하지만,
   담당자 2·3이 Engine B를 완성하면 이 함수 '속'만 진짜 호출로 바꾼다.
   반환 형식(list[WorkerOutput])만 지키면 부르는 쪽은 한 줄도 안 바뀐다.

   교체 시:   return engine_b.run(domain, req)   ← 이 한 줄만 교체

⚠️ 지금은 각 워커의 result가 고정 가짜다. 실제로는 워커가 Tool로 데이터를
   가져와 판단한다. 다만 '도메인 → 워커 세트' 매핑은 실제 설계 그대로 반영해둔다.
"""

from __future__ import annotations

from app.schemas.request import AgentRequest
from app.schemas.state import Domain, WorkerOutput

# 도메인 → 병렬 실행할 워커(dimension) 세트. 실제 설계와 동일.
WORKER_SETS: dict[Domain, list[str]] = {
    Domain.vacation: ["risk", "staffing"],
    Domain.project: ["priority", "risk", "cost"],
    Domain.hcm: ["staffing"],
    Domain.meeting: ["slot_a", "slot_b", "slot_c"],
}

# 각 dimension이 낼 가짜 결과 (지금은 고정, 실제로는 워커가 Tool로 판단)
_MOCK: dict[str, WorkerOutput] = {
    "priority": WorkerOutput(dimension="priority", result={"top_task": "Kafka 구축"},
                             reasoning="의존 task가 가장 많음 (mock)", confidence=0.85),
    "risk": WorkerOutput(dimension="risk", result={"level": "high", "bottleneck": "이하늘"},
                         reasoning="유일 담당 task 존재 (mock)", confidence=0.9),
    "cost": WorkerOutput(dimension="cost", result={"execution_rate": 62},
                         reasoning="집행률 62%, 여유 있음 (mock)", confidence=0.8),
    "staffing": WorkerOutput(dimension="staffing", result={"level": "medium"},
                             reasoning="팀 평균 업무량 (mock)", confidence=0.7),
}


def analyze(domain: Domain, req: AgentRequest) -> list[WorkerOutput]:
    """도메인에 맞는 워커 세트를 '병렬로' 실행한 결과를 반환한다. (현재 mock)

    실제로는 이 안에서 Router→Context→Worker병렬→Validator→Synthesis 가 돈다.
    지금은 도메인별 워커 세트만 골라 고정 가짜를 돌려준다.
    """
    dims = WORKER_SETS.get(domain, ["risk"])
    return [_MOCK[d] for d in dims if d in _MOCK]
