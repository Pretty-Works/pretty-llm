# app/tools/budget_tool.py
"""예산/지출 읽기 툴 (Engine B cost 워커용).

`spent` 는 확정 지출, `committed` 는 결재 진행 중이라 아직 안 나갔지만 사실상 묶인 금액이다.
잔여 예산을 볼 때 committed 를 빼먹으면 실제보다 여유가 있어 보이므로 항상 같이 본다.
다만 내부도구에 결재 조회가 없어 committed 는 늘 0 이다 — 잔액이 실제보다 커 보인다는 뜻이고,
컨텍스트 렌더링이 그 사실을 함께 낸다.

조회 창구는 clients/backend.py 하나다 (2026-08-07 통일 — X-Run-Id 는 run_context 로 전파).
★ 8/12 — mock 모드라고 여기서 곧장 우회하지 않는다. backend.get() 이
mock_backend=True 일 때 이미 _mock_get() 을 쓰므로, 그걸 또 우회하면 Engine A 화면이
보는 mock 예산과 Engine B cost 워커가 보는 예산이 서로 다른 프로젝트 것처럼 어긋난다.
★ 8/11 — 그렇다고 실패 시 demo_data 로 메우지도 않는다. 조회 실패는 **빈 결과**다.
  결재 대기(approvals) 조회 툴은 대응 API 가 아예 없어 삭제했다.
"""

import json
from typing import Any

from langchain_core.tools import tool

from app.clients.backend import backend
from app.common.run_context import current_run_id
from app.utils.logger import get_logger

log = get_logger("tools.budget")

_BUDGET_PATH = "/projects/{project_id}/budget"     # budget.summary
_EXPENSE_PATH = "/projects/{project_id}/expenses"  # expense.list


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _get(path: str, **params: Any) -> Any | None:
    """백엔드 조회. 창구는 backend.get() 하나다 (mock 이든 실백엔드든).

    run_id 부재·호출 실패면 None — 호출부가 '확인 못 함'으로 처리한다.
    픽스처로 메우지 않는다: 값을 지어내느니 없다고 답하는 쪽이 낫다.
    """
    run_id = current_run_id.get()
    if not run_id:
        log.warning("run_id 없이 내부도구 호출: %s — 조회 생략", path)
        return None
    try:
        return await backend.get(path, run_id, **params)
    except Exception as exc:  # 조회 실패로 분석을 죽이지는 않되, 값을 지어내지도 않는다
        log.warning("backend GET %s 실패 -> 빈 결과: %s", path, exc)
        return None


# ─── 조회 툴 (워커가 자율 호출) ───────────────────────────────────

@tool
async def get_project_budget(project_id: int) -> str:
    """프로젝트 예산 현황(총액·집행액·잔액·소진율)을 조회한다."""
    budget = await fetch_budget(project_id)
    if not budget:
        return _json({"error": "예산 정보를 조회하지 못했습니다.", "project_id": project_id})

    total = budget.get("total", 0) or 0
    spent = budget.get("spent", 0) or 0
    committed = budget.get("committed", 0) or 0
    return _json(
        {
            **budget,
            "remaining": total - spent - committed,
            "usage_ratio": round((spent + committed) / total, 3) if total else 0.0,
            "note": "결재 대기 금액 미반영 — 실제 잔액은 이보다 적을 수 있다",
        }
    )


@tool
async def list_project_expenses(project_id: int, limit: int = 20) -> str:
    """확정된 지출 내역을 최신순으로 조회한다. 어떤 항목이 예산을 갉아먹었는지 볼 때 쓴다."""
    raw = await _get(_EXPENSE_PATH.format(project_id=project_id))
    if raw is None:
        return _json({"error": "지출 내역을 조회하지 못했습니다.", "project_id": project_id,
                      "count": 0, "expenses": []})

    expenses = [
        {"id": e.get("expenseId"), "date": e.get("expenseDate"),
         "category": e.get("categoryLabel") or e.get("category"),
         "amount": e.get("amount"), "purpose": e.get("purpose"),
         "spender": e.get("spenderName")}
        for e in raw.get("expenses", [])
    ]
    expenses = sorted(expenses, key=lambda e: str(e.get("date", "")), reverse=True)[:limit]

    by_category: dict[str, int] = {}
    for expense in expenses:
        by_category[expense.get("category", "기타")] = by_category.get(
            expense.get("category", "기타"), 0
        ) + int(expense.get("amount", 0) or 0)

    return _json(
        {
            "project_id": project_id,
            "count": len(expenses),
            "by_category": by_category,
            "expenses": expenses,
        }
    )


# ─── 비-툴 진입점 (Context Builder 가 직접 호출) ──────────────────

async def fetch_budget(project_id: int) -> dict | None:
    """Context Builder 가 직접 쓰는 비-툴 진입점. 못 받으면 None — 지어내지 않는다."""
    raw = await _get(_BUDGET_PATH.format(project_id=project_id))
    if raw is None:
        return None
    # budget.summary → 내부 표현. committed 는 대응 API 가 없어 늘 0 이다.
    return {
        "project_id": project_id,
        "total": raw.get("targetBudget", 0),
        "spent": raw.get("spentAmount", 0),
        "committed": raw.get("committed", 0),
        "execution_rate": raw.get("executionRate"),
        "elapsed_rate": raw.get("elapsedRate"),
    }


BUDGET_TOOLS = [get_project_budget, list_project_expenses]
