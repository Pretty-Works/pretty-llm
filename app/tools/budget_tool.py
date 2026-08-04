# app/tools/budget_tool.py
"""예산/지출 읽기 툴 (Engine B cost 워커용).

`spent` 는 확정 지출, `committed` 는 결재 진행 중이라 아직 안 나갔지만 사실상 묶인 금액이다.
잔여 예산을 볼 때 committed 를 빼먹으면 실제보다 여유가 있어 보이므로 항상 같이 본다.

TODO(백엔드): 재무 API 경로가 아직 명세되지 않아 아래 경로는 가정값이다.
             Notion API 명세서에 확정되면 _BUDGET_PATH 만 고치면 된다.
"""

import json
from typing import Any

from langchain_core.tools import tool

from app.common import backend_client
from app.common.backend_client import BackendUnavailable
from app.tools import demo_data
from app.utils.logger import get_logger

log = get_logger("tools.budget")

_BUDGET_PATH = "/api/v1/projects/{project_id}/budget"
_EXPENSE_PATH = "/api/v1/projects/{project_id}/expenses"
_APPROVAL_PATH = "/api/v1/projects/{project_id}/approvals"


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


# ─── 조회 툴 (워커가 자율 호출) ───────────────────────────────────

@tool
def get_project_budget(project_id: int) -> str:
    """프로젝트 예산 현황(총액·집행액·결재중 금액·잔액·소진율)을 조회한다."""
    budget = fetch_budget(project_id)
    if not budget:
        return _json({"error": "예산 정보가 없습니다.", "project_id": project_id})

    total = budget.get("total", 0) or 0
    spent = budget.get("spent", 0) or 0
    committed = budget.get("committed", 0) or 0
    return _json(
        {
            **budget,
            "remaining": total - spent - committed,
            "usage_ratio": round((spent + committed) / total, 3) if total else 0.0,
        }
    )


@tool
def list_project_expenses(project_id: int, limit: int = 20) -> str:
    """확정된 지출 내역을 최신순으로 조회한다. 어떤 항목이 예산을 갉아먹었는지 볼 때 쓴다."""
    try:
        expenses = backend_client.get(_EXPENSE_PATH.format(project_id=project_id))
    except BackendUnavailable:
        expenses = demo_data.list_expenses(project_id)

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


@tool
def list_pending_approvals(project_id: int) -> str:
    """결재 진행 중인(아직 집행 전이지만 확정에 가까운) 지출 문서를 조회한다."""
    try:
        approvals = backend_client.get(_APPROVAL_PATH.format(project_id=project_id))
    except BackendUnavailable:
        approvals = demo_data.list_pending_approvals(project_id)
    total = sum(int(a.get("amount", 0) or 0) for a in approvals)
    return _json({"project_id": project_id, "count": len(approvals), "total_amount": total, "approvals": approvals})


# ─── 비-툴 진입점 (Context Builder 가 직접 호출) ──────────────────

def fetch_budget(project_id: int) -> dict | None:
    """Context Builder 가 직접 쓰는 비-툴 진입점."""
    try:
        return backend_client.get(_BUDGET_PATH.format(project_id=project_id))
    except BackendUnavailable:
        return demo_data.get_budget(project_id)


BUDGET_TOOLS = [get_project_budget, list_project_expenses, list_pending_approvals]
