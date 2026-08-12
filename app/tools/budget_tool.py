# app/tools/budget_tool.py
"""예산/지출 읽기 툴 (Engine B cost 워커용).

`spent` 는 확정 지출, `committed` 는 결재 진행 중이라 아직 안 나갔지만 사실상 묶인 금액이다.
잔여 예산을 볼 때 committed 를 빼먹으면 실제보다 여유가 있어 보이므로 항상 같이 본다.

조회 창구는 clients/backend.py 하나다 (2026-08-07 통일 — X-Run-Id 는 run_context 로 전파).
★ 8/12 수정 — mock 모드라고 여기서 곧장 demo_data 로 안 간다(project_query.py 와
같은 이유 — backend.get() 은 mock_backend=True 여도 이미 _mock_get() 픽스처를
쓰므로, 그걸 우회하면 Engine A 화면이 보는 mock 프로젝트 예산과 Engine B cost
워커가 보는 예산이 서로 다른 프로젝트 것처럼 어긋난다). budget·expenses 는
_mock_get() 이 이미 커버하니 mock 이든 실백엔드든 항상 backend.get() 을 먼저
시도하고, run_id 부재·호출 실패일 때만 demo_data 로 폴백한다.
결재 중(approvals)은 아직 내부도구 명세 자체가 없어 여전히 픽스처 전용이다.
"""

import json
from typing import Any

from langchain_core.tools import tool

from app.clients.backend import backend
from app.common.run_context import current_run_id
from app.tools import demo_data
from app.utils.logger import get_logger

log = get_logger("tools.budget")

_BUDGET_PATH = "/projects/{project_id}/budget"     # budget.summary
_EXPENSE_PATH = "/projects/{project_id}/expenses"  # expense.list


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _get(path: str, **params: Any) -> Any | None:
    """백엔드 조회(mock 이든 실백엔드든 backend.get() 창구 하나로 통일). run_id
    부재·호출 실패면 None — 호출부가 demo_data 픽스처로 폴백한다."""
    run_id = current_run_id.get()
    if not run_id:
        log.warning("run_id 없이 내부도구 호출: %s — 픽스처 폴백", path)
        return None
    try:
        return await backend.get(path, run_id, **params)
    except Exception as exc:  # 조회 실패로 분석을 죽이지 않는다 (폴백 원칙)
        log.warning("backend GET %s 실패 -> 픽스처 폴백: %s", path, exc)
        return None


# ─── 조회 툴 (워커가 자율 호출) ───────────────────────────────────

@tool
async def get_project_budget(project_id: int) -> str:
    """프로젝트 예산 현황(총액·집행액·결재중 금액·잔액·소진율)을 조회한다."""
    budget = await fetch_budget(project_id)
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
async def list_project_expenses(project_id: int, limit: int = 20) -> str:
    """확정된 지출 내역을 최신순으로 조회한다. 어떤 항목이 예산을 갉아먹었는지 볼 때 쓴다."""
    raw = await _get(_EXPENSE_PATH.format(project_id=project_id))
    if raw is None:
        expenses = demo_data.list_expenses(project_id)
    else:  # expense.list → 픽스처 모양으로 정규화
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


@tool
async def list_pending_approvals(project_id: int) -> str:
    """결재 진행 중인(아직 집행 전이지만 확정에 가까운) 지출 문서를 조회한다."""
    # 내부도구 명세 없음 — BE 명세 확정 전까지 픽스처 전용
    approvals = demo_data.list_pending_approvals(project_id)
    total = sum(int(a.get("amount", 0) or 0) for a in approvals)
    return _json({"project_id": project_id, "count": len(approvals), "total_amount": total, "approvals": approvals})


# ─── 비-툴 진입점 (Context Builder 가 직접 호출) ──────────────────

async def fetch_budget(project_id: int) -> dict | None:
    """Context Builder 가 직접 쓰는 비-툴 진입점."""
    raw = await _get(_BUDGET_PATH.format(project_id=project_id))
    if raw is None:
        return demo_data.get_budget(project_id)
    # budget.summary → 내부 표현. committed 는 명세에 없어 0 (approvals 명세 나오면 합류)
    return {
        "project_id": project_id,
        "total": raw.get("targetBudget", 0),
        "spent": raw.get("spentAmount", 0),
        "committed": raw.get("committed", 0),
        "execution_rate": raw.get("executionRate"),
        "elapsed_rate": raw.get("elapsedRate"),
    }


BUDGET_TOOLS = [get_project_budget, list_project_expenses, list_pending_approvals]
