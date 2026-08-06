# app/engine_b/replan_service.py
"""
Replan 오케스트레이션 (텍스트 방식 + BE 저장) — 담당자3

  propose(request, conversation_id, run_id) -> str
     3안 생성 → apply_builder ×3 → ReplanSaveRequest → BE 저장(replanId 수신)
     → 포인터 보관 → 채팅 텍스트 반환
  apply_from_text(conversation_id, text, run_id, approval_token) -> str
     자유 텍스트("2번"/"일정연장으로 해줘")에서 고른 안 → BE 반영요청(replanId + scenarioType)

★ 3안 실데이터는 BE 저장. 여기 로컬엔 conversationId→replanId 포인터만(suggestion_store).
★ 저장은 승인 불필요(제안 저장), 반영은 승인 필요(실 DB write). approval_token 은 BE 발급.
★ 정책: 조정안 변환 실패 시 그 안은 제외하고, 반영 가능한 안만 저장·제시한다.
"""
from __future__ import annotations

import re

from app.clients.backend import backend, canonical_json
from app.common.exceptions import ApprovalRequiredError
from app.engine_b.analysis_router import route
from app.engine_b.apply_builder import build_apply_request
from app.engine_b.replan import run_replan
from app.engine_b.scenario_executor import SCENARIO_LABELS
from app.engine_b.suggestion_store import PendingReplan, store
from app.schemas.replan import (
    Comparison, ReplanApplyRequest, ReplanSaveRequest, ReplanScenario,
)
from app.schemas.state import (
    AnalysisPlan, AnalysisRequest, Mode, SynthesisResult, TradeoffResult,
)
from app.tools.registry import build_request
from app.utils.logger import get_logger

log = get_logger("engine_b.replan_service")

_LABEL_TO_ID = {label: sid for sid, label in SCENARIO_LABELS.items()}


# ─── 생성: 3안 → BE 저장 → 텍스트 ────────────────────────────────

async def propose(
    request: AnalysisRequest,
    conversation_id: int | str,
    run_id: str,
    plan: AnalysisPlan | None = None,
) -> str:
    """3안 생성 → 변환 → BE 저장 → 채팅에 보여줄 텍스트 반환."""
    plan = plan or await route(request, force_mode=Mode.replan)
    result = await run_replan(request, plan)
    project_id = _project_id(request, plan)

    by_id = {c.scenario_type: c for c in result.tradeoff.comparisons}
    scenarios: list[ReplanScenario] = []
    order: list[str] = []
    for s in result.scenarios:
        built = build_apply_request(s)
        if not built.ok:
            log.warning("조정안 %s 제외(변환 실패): %s", s.scenario_id, built.rejected)
            continue
        scenarios.append(ReplanScenario(
            scenarioType=s.scenario_id,
            comparison=_comparison(by_id.get(s.scenario_id), s),
            applyRequest=built.apply_request,
        ))
        order.append(s.scenario_id)

    if not scenarios:
        return "지금은 반영 가능한 재계획 안을 만들지 못했어요. 잠시 후 다시 시도해 주세요."

    save = ReplanSaveRequest(scenarios=scenarios)
    method, path, params = build_request(
        "replan_save", {"projectId": project_id, **save.model_dump(mode="json", by_alias=True)})
    saved = await backend.write(method, path, run_id=run_id,
                                approval_token=None, body=canonical_json(params))
    replan_id = str((saved or {}).get("replanId", ""))

    store.save(PendingReplan(key=str(conversation_id), replan_id=replan_id,
                             project_id=project_id, scenario_types=order))
    log.info("replan 저장: conv=%s replanId=%s scenarios=%s", conversation_id, replan_id, order)
    return _render(scenarios, result.tradeoff)


# ─── 선택 + 반영: 자유 텍스트 → BE 반영요청 ──────────────────────

async def apply_from_text(
    conversation_id: int | str,
    text: str,
    run_id: str,
    approval_token: str | None = None,
) -> str:
    pending = store.load(str(conversation_id))
    if pending is None:
        return "진행 중인 재계획 제안이 없어요. 먼저 재계획을 요청해 주세요."

    scenario_type = parse_selection(text, pending.scenario_types)
    if scenario_type is None:
        return "어느 방안인지 번호(1~3)나 이름(일정연장/인력추가/범위축소)으로 알려주세요."

    if not approval_token:
        # WRITE 는 승인 토큰 없이는 못 나간다(BE 가 AGENT_014 로 거부).
        raise ApprovalRequiredError("재계획 반영에는 승인 토큰이 필요합니다.")

    apply = ReplanApplyRequest(scenarioType=scenario_type)
    method, path, params = build_request("replan_apply", {
        "projectId": pending.project_id,
        "replanId": pending.replan_id,
        **apply.model_dump(mode="json"),
    })
    await backend.write(method, path, run_id=run_id,
                        approval_token=approval_token, body=canonical_json(params))
    store.pop(str(conversation_id))

    label = SCENARIO_LABELS.get(scenario_type, scenario_type)
    log.info("replan 반영요청: conv=%s replanId=%s scenario=%s",
             conversation_id, pending.replan_id, scenario_type)
    return f"'{label}' 방안으로 반영했어요."


# ─── 렌더 / 파싱 / 헬퍼 ───────────────────────────────────────────

def _comparison(c, scenario: SynthesisResult) -> Comparison:
    if c is None:
        return Comparison(summary=scenario.summary or "")
    return Comparison(summary=c.summary or scenario.summary or "", risk=c.risk_level,
                      scheduleRecovery=c.schedule_recovery, cost=c.cost_impact)


def _render(scenarios: list[ReplanScenario], tradeoff: TradeoffResult) -> str:
    rec = tradeoff.recommended_scenario
    rec_label = SCENARIO_LABELS.get(rec, rec)
    lines = [f"재계획 방안 {len(scenarios)}가지예요. 추천은 '{rec_label}'이에요.", ""]
    for i, sc in enumerate(scenarios, 1):
        label = SCENARIO_LABELS.get(sc.scenarioType, sc.scenarioType)
        tag = " (추천)" if sc.scenarioType == rec else ""
        cmp = sc.comparison
        lines.append(f"{i}) {label}{tag}")
        if cmp.scheduleRecovery or cmp.cost or cmp.risk:
            lines.append(f"   일정회복 {cmp.scheduleRecovery} · 비용 {cmp.cost} · 리스크 {cmp.risk}")
        if cmp.summary:
            lines.append(f"   {cmp.summary}")
    if tradeoff.tradeoffs:
        lines += ["", "감수사항: " + ", ".join(tradeoff.tradeoffs)]
    lines += ["", '원하는 번호나 이름을 말해주세요 (예: "1번" 또는 "일정연장으로 해줘").']
    return "\n".join(lines)


def parse_selection(text: str, scenario_types: list[str]) -> str | None:
    """'2번' / '일정연장으로 해줘' / 'extend' → scenarioType."""
    raw = text or ""
    low = raw.lower()
    m = re.search(r"([1-9])\s*번", raw)
    if m:
        i = int(m.group(1)) - 1
        if 0 <= i < len(scenario_types):
            return scenario_types[i]
    for label, sid in _LABEL_TO_ID.items():
        if label in raw and sid in scenario_types:
            return sid
    for sid in scenario_types:
        if sid.lower() in low:
            return sid
    m = re.fullmatch(r"\s*([1-9])\s*", raw)
    if m:
        i = int(m.group(1)) - 1
        if 0 <= i < len(scenario_types):
            return scenario_types[i]
    return None


def _project_id(request: AnalysisRequest, plan: AnalysisPlan) -> int | None:
    if request.ui_context.project_id:
        return request.ui_context.project_id
    if plan.entities.project_ids:
        return plan.entities.project_ids[0]
    return None
