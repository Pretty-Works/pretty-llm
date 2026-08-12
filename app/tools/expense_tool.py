"""
지출·예산 도구 — 예산 현황(budget.summary) · 지출 내역(expense.list) · 등록(expense.create)

expense.create 는 AUTO_FORBIDDEN(돈) — auto 모드여도 항상 사람 승인이다.
금액 해석("12만원"→120000)이 이 도메인의 최대 리스크라, 금액은 반드시
승인 카드에서 사용자가 확인하게 된다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend
from app.common.exceptions import WriteRejectedError
from app.tools.registry import RunContext
from app.tools.write_exec import execute_write

CATEGORIES = "TRANSPORT·MEAL·SOFTWARE·OFFICE_SUPPLY·EDUCATION·LABOR·OUTSOURCING·INFRA·ETC"


@tool
async def budget_summary(projectId: int, runtime: ToolRuntime[RunContext]) -> str:
    """프로젝트의 예산·집행액·카테고리별 집계를 조회한다.

    "예산 얼마 썼어?" 류 질문과, 지출 등록 전 상황 파악에 쓴다.
    건별 내역이 필요하면 expense_list 를 쓴다.
    """
    r = await backend.get(f"/projects/{projectId}/budget", run_id=runtime.context.run_id)
    cats = " / ".join(f"{c['categoryLabel']} {c['amount']:,}원({c['share']}%)"
                      for c in r["byCategory"])
    if not r["targetBudget"]:
        result = (f"목표 예산이 설정되지 않은 프로젝트입니다 (제한 없음). "
                  f"현재까지 지출 {r['spentAmount']:,}원, {r['expenseCount']}건. 분류: {cats}")
        runtime.context.known_facts[f"budget_summary:{projectId}"] = result
        return result
    warn = " ⚠️ 집행률이 기간 경과율보다 높음 — 소진 위험" \
        if r["executionRate"] > r["elapsedRate"] else ""
    result = (f"예산 {r['targetBudget']:,}원 중 {r['spentAmount']:,}원 집행 "
              f"(집행률 {r['executionRate']}% vs 기간 경과 {r['elapsedRate']}%){warn}. "
              f"잔액 {r['remainingAmount']:,}원. 분류: {cats}")
    runtime.context.known_facts[f"budget_summary:{projectId}"] = result
    return result


@tool
async def expense_list(projectId: int, sort: str,
                       runtime: ToolRuntime[RunContext]) -> str:
    """프로젝트의 지출 내역을 조회한다.

    projectId: 프로젝트 ID
    sort: DATE_DESC(최신순) | AMOUNT_DESC(금액 큰 순 — "제일 큰 지출" 질문용)
    """
    r = await backend.get(f"/projects/{projectId}/expenses",
                          run_id=runtime.context.run_id, sort=sort)
    if not r["expenses"]:
        result = "등록된 지출이 없습니다."
        runtime.context.known_facts[f"expense_list:{projectId}"] = result
        return result
    lines = [f"- [{e['expenseId']}] {e['expenseDate']} {e['categoryLabel']} · "
             f"{e['merchant']} · {e['amount']:,}원 ({e['spenderName']})"
             for e in r["expenses"]]
    result = f"지출 {r['totalCount']}건:\n" + "\n".join(lines)
    runtime.context.known_facts[f"expense_list:{projectId}"] = result
    return result


@tool
async def expense_create(projectId: int, expenseDate: str, category: str,
                         merchant: str, purpose: str, amount: int,
                         runtime: ToolRuntime[RunContext]) -> str:
    """지출을 등록한다. 등록 전 사용자 승인을 받는다 (auto 모드여도 항상 사람).

    금액은 원 단위 정수만 — "12만원"은 120000 이다. 해석이 조금이라도
    애매하면 지어내지 말고 되물어라.
    사용일은 프로젝트 기간 안이어야 한다 (project_search 로 기간 확인).

    projectId:   프로젝트 ID
    expenseDate: 사용일 (YYYY-MM-DD)
    category:    TRANSPORT·MEAL·SOFTWARE·OFFICE_SUPPLY·EDUCATION·LABOR·OUTSOURCING·INFRA·ETC
    merchant:    사용처 (100자)
    purpose:     사용 목적 (255자)
    amount:      금액 (원, 1 이상 정수)
    """
    ctx = runtime.context
    args = {"projectId": projectId, "expenseDate": expenseDate, "category": category,
            "merchant": merchant, "purpose": purpose, "amount": amount}
    try:
        r = await execute_write("expense_create", args, ctx)
    except WriteRejectedError as e:
        return str(e)
    over = ""
    if r.get("executionRateAfter") and r["executionRateAfter"] > 100:
        over = f" ⚠️ 이 지출로 집행률이 {r['executionRateAfter']}% — 예산 초과입니다."
    return f"지출 {amount:,}원을 등록했습니다 (expenseId={r['expenseId']}).{over}"
