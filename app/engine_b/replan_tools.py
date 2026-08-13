# app/engine_b/replan_tools.py
"""
Replan HITL 도구 — 3안 생성(분석) → 저장(승인) → 반영(승인), 3단계를 엔진A와
같은 "쓰기 도구 승인" 패턴으로 노출한다.

★ 2026-08-09 BE 스펙 전면 개정 — 예전엔 저장(replan.save)이 AUTO_ALLOWED 라
  propose_replan_scenarios 안에서 곧장 backend.write 를 불렀다. 이제 저장도
  승인이 필요해서(외부 텍스트를 읽고 만든 계획이 "공식 기록"이 되는 시점이라
  사람이 한 번 본다) 저장을 별도 @tool(replan_save)로 뺐다 — registry.is_write()
  가 이름으로 자동 판별해 HumanInTheLoopMiddleware 가 승인 게이트를 건다.

  세 도구:
    propose_replan_scenarios — 3안 분석만 한다. DB 안 건드림. 승인 불필요.
    replan_save               — 저장(DB write, HITL 승인 필요). propose 결과의
                                 scenarioType·summary·risk·operations 를 그대로
                                 옮겨 적어 호출해야 한다(재작성·요약 금지) — 승인
                                 카드에 뜨는 내용이 그대로 저장되는 내용이어야
                                 하므로, 인자를 바꿔치기하면 사용자가 못 본 내용이
                                 저장된다.
    replan_apply               — 저장분 중 선택된 안을 실제 반영(HITL 승인 필요).
                                 replanId·projectId·scenarioType 만 넘긴다 —
                                 operations 는 BE 가 저장분에서 꺼내 쓴다(재승인
                                 사이 조작 방지).

  3안 실데이터(operations)는 예전처럼 숨기지 않는다 — replan_save 인자로 LLM 이
  직접 채워야 하므로(저장 자체가 이제 승인 대상 쓰기 도구), propose 결과 텍스트에
  전부 펼쳐 보여준다. 대신 replan_apply 는 여전히 identifiers(replanId·projectId)
  만 옮겨 적으면 된다 — 예전 관례(project_search → meeting_create 처럼 식별자만
  이어붙이는 방식) 그대로.
"""
from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from pydantic import ValidationError

from app.clients.backend import backend
from app.common.exceptions import WriteRejectedError
from app.engine_b.analysis_router import route
from app.engine_b.apply_builder import build_operations
from app.engine_b.scenario_executor import SCENARIO_LABELS
from app.engine_b.scenario_executor import run as run_scenarios
from app.engine_b.tradeoff import run as run_tradeoff
from app.schemas.replan import ReplanSaveRequest, ReplanScenario
from app.schemas.state import (
    AnalysisPlan, AnalysisRequest, Mode, SynthesisResult, TradeoffResult, UIContext,
)
from app.tools.registry import RunContext
from app.tools.write_exec import execute_write
from app.utils.logger import get_logger

log = get_logger("engine_b.replan_tools")

PROPOSE_LIMIT = 2   # 최초 1회 + 피드백/수정 반영 재생성 1회. 초과하면 재생성 거부.

# tradeoff.py 가 내는 리스크는 한글(높음/중간/낮음) — BE RiskLevel(LOW/MEDIUM/HIGH)로 변환.
_RISK_KO_TO_EN = {"높음": "HIGH", "중간": "MEDIUM", "낮음": "LOW"}


@tool
async def propose_replan_scenarios(query: str, project_id: int | None = None,
                                   runtime: ToolRuntime[RunContext] = None) -> str:
    """재계획 조정안 3개(일정조정/인력재배치/범위축소)를 분석해 비교 결과를 돌려준다.
    DB 를 바꾸지 않는다(분석만) — 저장하려면 이 결과를 그대로 옮겨 적어 replan_save 를
    불러야 한다.

    query 에는 재계획이 필요한 상황과 목적을 한두 문장으로 정리해 넣어라
    (예: "그룹웨어 리뉴얼 프로젝트 일정이 2주 밀렸는데 어떻게 조정할지").
    project_id 에는 대화 맨 앞의 화면 컨텍스트("현재 화면 / 입력된 폼 값")나
    사용자 발화에 프로젝트 ID가 숫자로 명시돼 있으면 그 값을 그대로 넘겨라 —
    이름만 아는 경우엔 생략해도 된다(라우터가 텍스트에서 이름으로는 id를
    지어내지 않으므로, id를 아는 경우엔 반드시 이 인자로 넘기는 편이 안전하다).

    사용자가 3안을 전부 거절했거나(예: "다 별로야") 특정 안의 수정을 원하면
    (예: "2번인데 예산은 그대로 둬줘"), 그 방향을 반영해 query 를 다시 써서 이
    도구를 한 번 더 부를 수 있다 — 단, 재생성은 세션당 딱 1회로 제한된다(비용
    문제). 한도를 넘으면 이 도구는 아무 계산도 하지 않고 거부 문구만 돌려준다
    — 그러면 지금까지 만든 안 중에서 골라달라고 사용자에게 안내하라.
    """
    messages = (runtime.state or {}).get("messages", []) if runtime else []
    called = sum(1 for m in messages
                 if getattr(m, "type", "") == "tool"
                 and getattr(m, "name", "") == "propose_replan_scenarios")
    if called >= PROPOSE_LIMIT:
        return (f"재계획 안 재생성 한도({PROPOSE_LIMIT}회, 최초 포함)를 넘어 "
                "더 만들 수 없습니다. 지금까지 만든 안 중에서 골라달라고 안내하세요.")

    ctx = runtime.context
    me = await backend.get("/me", run_id=ctx.run_id)
    request = AnalysisRequest(
        query=query, user_id=me["userId"],
        ui_context=UIContext(project_id=project_id) if project_id else UIContext(),
    )

    plan: AnalysisPlan = await route(request, force_mode=Mode.replan)

    # project_id 를 못 찾으면 저장 경로(/projects/{projectId}/replans)를 못 만든다 —
    # 분석부터 하기 전에 먼저 확인해 헛수고(워커 5개 × 3안)를 막는다.
    resolved_project_id = _project_id(request, plan)
    if resolved_project_id is None:
        return ("어느 프로젝트의 재계획인지 특정하지 못했습니다. ask_user 로 프로젝트를 "
                "먼저 확인한 뒤, project_id 인자를 채워 다시 호출하세요. (재생성 "
                "한도에는 포함되지 않습니다.)")

    def _progress(text: str) -> None:
        if runtime and runtime.stream_writer:
            runtime.stream_writer({"text": text})   # → hitl._drive 가 step 으로 방출

    scenarios_result: list[SynthesisResult] = await run_scenarios(
        request, plan, on_progress=_progress)
    _progress("세 가지 안을 비교하고 있어요")
    tradeoff: TradeoffResult = await run_tradeoff(scenarios_result)

    by_id = {c.scenario_type: c for c in tradeoff.comparisons}
    scenarios: list[ReplanScenario] = []
    # ★ 8/12 추가 — 이전엔 여기서 제외된 안을 서버 로그에만 남기고 조용히 지웠다.
    #   그러면 3안을 만들어도 1~2안만 사용자에게 보이는데, LLM 도 사용자도 왜
    #   줄었는지 알 방법이 없었다("3안 생성해줬는데 1안만 선택지에 있어" 증상).
    #   여기서 이유를 모아 _render() 결과 텍스트에 실어 LLM 이 사용자에게
    #   솔직히 알릴 수 있게 한다.
    dropped: list[str] = []
    for s in scenarios_result:
        label = SCENARIO_LABELS.get(s.scenario_id, s.scenario_id)
        built = build_operations(s)
        if not built.ok:
            log.warning("조정안 %s 제외(변환 실패): %s", s.scenario_id, built.rejected)
            reason = built.rejected[0]["reason"] if built.rejected else (
                "제안된 변경 사항을 실제 반영 가능한 작업으로 바꾸지 못함")
            dropped.append(f"{label}: {reason}")
            continue
        cmp = by_id.get(s.scenario_id)
        try:
            scenarios.append(ReplanScenario(
                scenarioType=s.scenario_id,
                summary=(cmp.summary if cmp and cmp.summary else "") or s.summary
                        or s.headline or "재계획 조정안",
                risk=_risk(cmp.risk_level if cmp else ""),
                operations=built.operations,
            ))
        except ValidationError as e:
            log.warning("조정안 %s 제외(스키마 검증 실패): %s", s.scenario_id, e)
            dropped.append(f"{label}: 저장 형식 검증 실패({_first_pydantic_error(e)})")

    if not scenarios:
        return ("지금은 반영 가능한 재계획 안을 만들지 못했습니다. 잠시 후 다시 "
                "시도해달라고 안내하세요.")

    log.info("replan 분석 완료: projectId=%s scenarios=%s dropped=%s",
             resolved_project_id, [s.scenarioType for s in scenarios], dropped)
    return _render(resolved_project_id, scenarios, tradeoff, dropped)


@tool
async def replan_save(projectId: int, reason: str, scenarios: list[ReplanScenario],
                      runtime: ToolRuntime[RunContext] = None) -> str:
    """재계획 조정안을 저장한다 — DB write, 사용자 승인 필요(미들웨어가 자동으로
    승인 게이트를 건다 — 별도 처리 없이 그냥 호출하면 된다).

    projectId · scenarios(scenarioType·summary·risk·operations 전부)는
    propose_replan_scenarios 결과에 나온 값을 그대로 옮겨 적어라 — 요약하거나
    새로 만들지 마라. 승인 카드에 뜨는 내용이 곧 저장되는 내용이다.
    reason 에는 왜 재계획이 필요한지 한 문장으로 적어라(사용자 발화 근거).
    """
    ctx = runtime.context
    try:
        save = ReplanSaveRequest(projectId=projectId, reason=reason, scenarios=scenarios)
    except ValidationError as e:
        return (f"저장 형식이 올바르지 않습니다: {e}. propose_replan_scenarios 결과를 "
                "다시 확인해 옮겨 적으세요.")

    args = save.model_dump(mode="json", by_alias=True)
    try:
        saved = await execute_write("replan_save", args, ctx)
    except WriteRejectedError as e:
        return str(e)

    replan_id = str((saved or {}).get("replanId", ""))
    log.info("replan 저장: replanId=%s projectId=%s scenarios=%s",
             replan_id, projectId, [s.scenarioType for s in save.scenarios])
    return (f"[replanId={replan_id} projectId={projectId}] 저장 완료. 사용자가 고른 안의 "
            "scenarioType 과 이 replanId·projectId 를 replan_apply 인자로 그대로 "
            "옮겨 적어 반영을 요청하라.")


@tool
async def replan_apply(replanId: int, projectId: int, scenarioType: str,
                       runtime: ToolRuntime[RunContext] = None) -> str:
    """저장된 재계획 안 중 선택된 것을 실제로 반영한다 — 프로젝트 일정·인원·범위에
    DB write. 사용자 승인 필요(미들웨어가 자동으로 승인 게이트를 건다).

    replanId · projectId 는 replan_save 결과에 적힌 값을 그대로 옮겨 적어라 — 새로
    지어내지 마라. operations 는 다시 안 보낸다 — BE 가 저장분에서 그대로 꺼내 쓴다.
    scenarioType 은 REALLOCATE(인력재배치) | EXTEND(일정조정) | REDUCE_SCOPE(범위축소)
    중 사용자가 실제로 고른 것.
    """
    ctx = runtime.context
    args = {"projectId": projectId, "replanId": replanId, "scenarioType": scenarioType}
    try:
        await execute_write("replan_apply", args, ctx)
    except WriteRejectedError as e:
        return str(e)

    label = SCENARIO_LABELS.get(scenarioType, scenarioType)
    return f"'{label}' 방안으로 반영했습니다."


# ─── 렌더 / 헬퍼 ────────────────────────────────────────────────

def _risk(risk_ko_or_en: str) -> str:
    """tradeoff 가 내는 한글(높음/중간/낮음)을 BE RiskLevel(HIGH/MEDIUM/LOW)로.
    이미 영문이거나 못 알아들으면 MEDIUM 으로 안전하게 폴백한다."""
    v = (risk_ko_or_en or "").strip()
    if v.upper() in ("LOW", "MEDIUM", "HIGH"):
        return v.upper()
    return _RISK_KO_TO_EN.get(v, "MEDIUM")


def _render(project_id: int, scenarios: list[ReplanScenario], tradeoff: TradeoffResult,
           dropped: list[str] | None = None) -> str:
    """LLM 이 읽을 결과 텍스트. replan_save 인자로 그대로 옮겨 적어야 하므로
    operations 까지 전부 펼쳐 보여준다(예전엔 replanId 만 노출했지만, 이제 저장
    자체를 LLM 이 도구 인자로 채워야 해서 숨길 수 없다)."""
    rec = tradeoff.recommended_scenario
    rec_label = SCENARIO_LABELS.get(rec, rec)
    lines = [f"[projectId={project_id}]",
             f"재계획 방안 {len(scenarios)}가지. 추천은 '{rec_label}'.", ""]
    for i, sc in enumerate(scenarios, 1):
        label = SCENARIO_LABELS.get(sc.scenarioType, sc.scenarioType)
        tag = " (추천)" if sc.scenarioType == rec else ""
        lines.append(f"{i}) scenarioType={sc.scenarioType} — {label}{tag} risk={sc.risk}")
        lines.append(f"   summary: {sc.summary}")
        lines.append("   operations:")
        for op in sc.operations:
            lines.append(f"     - {op.model_dump(mode='json', by_alias=True, exclude_none=True)}")
    if tradeoff.tradeoffs:
        lines += ["", "감수사항: " + ", ".join(tradeoff.tradeoffs)]
    if dropped:
        # ★ 8/12 추가 — 3안 중 일부가 반영 가능한 형태로 안 만들어졌을 때, 그 사실을
        #   숨기지 않는다. LLM 은 이걸 보고 사용자에게 "N개는 제외했다"고 먼저 알린
        #   뒤 남은 안으로 진행해야 한다(3안이 안 왔다고 곧장 재생성부터 하지 말 것 —
        #   재생성은 세션당 1회뿐이라 낭비하면 진짜 필요할 때 못 쓴다).
        lines += ["", f"⚠️ {len(dropped)}개 안은 반영 가능한 형태로 만들지 못해 제외했습니다:"]
        for d in dropped:
            lines.append(f"   - {d}")
    lines += ["",
              "저장하려면 replan_save(projectId, reason, scenarios) 를 위 scenarioType·"
              "summary·risk·operations 그대로 옮겨 적어 호출하라(재작성 금지)."]
    return "\n".join(lines)


def _first_pydantic_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if not errs:
        return "검증 실패"
    e = errs[0]
    loc = ".".join(str(x) for x in e.get("loc", ()))
    return f"{loc or '?'}: {e.get('msg', '검증 실패')}"


def _project_id(request: AnalysisRequest, plan: AnalysisPlan) -> int | None:
    """LLM 이 project_id 인자로 명시했으면 그걸 우선 쓰고(request.ui_context), 없으면
    라우터가 query 텍스트에서 뽑아낸 plan.entities.project_ids 로 폴백한다."""
    if request.ui_context.project_id:
        return request.ui_context.project_id
    if plan.entities.project_ids:
        return plan.entities.project_ids[0]
    return None
