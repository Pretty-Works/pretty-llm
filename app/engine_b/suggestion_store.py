# app/engine_b/suggestion_store.py
"""
Pending Replan 포인터 — 텍스트 방식에서 '3안 제시 → 다음 턴 선택' 을 잇는 최소 상태 (담당자3)

3안 실데이터(applyRequest 포함)는 BE 가 저장한다. 여기엔 다음 턴에 그 제안을 가리킬
'포인터'만 둔다: conversationId → { replanId, 표시된 scenarioType 순서 }.
  · replan_id     : 반영 요청 URL 에 필요
  · scenario_types: "2번" → scenarioType 매핑(표시 순서)

★ 이것도 프로세스 메모리라 다중 워커/재시작에 약하다. 완전 무상태로 가려면
  BE 에 'GET /projects/{pid}/replans/latest' 를 두고 이 포인터를 대체하면 된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PendingReplan:
    key: str                        # = conversationId (문자열)
    replan_id: str
    project_id: int | None
    scenario_types: list[str] = field(default_factory=list)   # 표시 순서


class PendingStore:
    def __init__(self) -> None:
        self._items: dict[str, PendingReplan] = {}

    def save(self, pending: PendingReplan) -> None:
        self._items[pending.key] = pending

    def load(self, key: str) -> PendingReplan | None:
        return self._items.get(key)

    def pop(self, key: str) -> PendingReplan | None:
        return self._items.pop(key, None)


store = PendingStore()
