# app/workers/registry.py
"""워커 레지스트리 — 도메인별 워커 세트.

Analysis Router 가 고른 도메인의 워커는 **항상 전부** 실행된다.
focus 는 강조점일 뿐이라 여기서 워커를 걸러내지 않는다. (설계 원칙)

담당자 3이 meeting 워커(Slot A·B·C)를 붙일 때는 각 워커 모듈에 `SPEC` 을 정의하고
아래 `register()` 를 호출하거나 `_REGISTRY` 에 항목을 추가하면 된다.
그래프 조립 코드는 손댈 필요가 없다.
"""

from app.schemas.state import Domain
from app.workers.base import WorkerSpec
from app.workers.hr import skill_fit, workload
from app.workers.meeting import adapter as meeting_adapter
from app.workers.project import cost, priority, risk

_REGISTRY: dict[str, list[WorkerSpec]] = {
    "project": [priority.SPEC, risk.SPEC, cost.SPEC],
    "hcm": [skill_fit.SPEC, workload.SPEC],
    "meeting": [meeting_adapter.SPEC],
    # "vacation": [...],  # 담당자 1 (Engine A 에서 위험 신호 시 넘어오는 경로)
}


def register(spec: WorkerSpec) -> None:
    """워커 추가. 같은 (도메인, 축)이 이미 있으면 교체한다."""
    specs = _REGISTRY.setdefault(spec.domain, [])
    for index, existing in enumerate(specs):
        if existing.dimension == spec.dimension:
            specs[index] = spec
            return
    specs.append(spec)


def specs_for_domains(domains: list[Domain] | list[str]) -> list[WorkerSpec]:
    """선택된 도메인들의 워커 전부. 아직 구현 안 된 도메인은 조용히 건너뛴다."""
    specs: list[WorkerSpec] = []
    for domain in dict.fromkeys(domains):
        specs.extend(_REGISTRY.get(domain, []))
    return specs


def spec_by_dimension(dimension: str) -> WorkerSpec | None:
    """Validator 가 특정 축만 재실행시킬 때 쓴다."""
    for specs in _REGISTRY.values():
        for spec in specs:
            if spec.dimension == dimension:
                return spec
    return None


def all_specs() -> list[WorkerSpec]:
    return [spec for specs in _REGISTRY.values() for spec in specs]


def unsupported_domains(domains: list[Domain] | list[str]) -> list[str]:
    """라우터가 골랐지만 아직 워커가 없는 도메인. 사용자에게 알려주기 위해 남긴다."""
    return [domain for domain in dict.fromkeys(domains) if not _REGISTRY.get(domain)]
