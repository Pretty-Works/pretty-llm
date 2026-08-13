# app/engine_b/analysis_router.py
"""Analysis Router — Engine B 진입점. LLM 을 딱 1번 불러 분석 계획을 정한다.

정하는 것은 세 가지뿐이다.
  domain : 어떤 도메인 워커 세트를 돌릴지
  focus  : 어느 축을 강조할지 (★ 실행 여부와 무관. 도메인 워커는 항상 전부 돈다)
  mode   : 현황 분석(analysis) 인지 재계획(replan) 인지

담당자 3의 Scenario Executor 도 이 라우터를 재사용한다.
단 조정안별 재분석에서는 mode 를 다시 판단할 필요가 없으므로 `force_mode` 로 고정한다.
"""

from langchain_core.messages import HumanMessage, SystemMessage

from app.common import llm_client
from app.config import get_settings
from app.prompts import analysis_router as prompts
from app.schemas.state import AnalysisPlan, AnalysisRequest, Domain, Mode, UIContext
from app.utils.logger import get_logger
from app.utils.parser import truncate

log = get_logger("engine_b.analysis_router")

# 라우터가 고를 수 있는 강조 축. 여기 없는 값은 버린다.
KNOWN_FOCUS = {"priority", "risk", "cost", "followup", "staffing", "my_week"}
KNOWN_DOMAINS = {"project", "hcm", "meeting", "vacation"}

# LLM 실패 시 폴백에 쓰는 키워드
_REPLAN_HINTS = ("조정", "재계획", "당겨", "미뤄", "변경해야", "빠졌", "줄여야", "안 몇", "대안")
_HCM_HINTS = ("누구", "인원", "담당자", "배치", "적임", "부하", "바쁜", "투입")
_MEETING_HINTS = ("회의", "미팅", "일정 잡", "시간대")
_VACATION_HINTS = ("휴가", "연차", "부재")


# ─── 공개 진입점 ──────────────────────────────────────────────────

async def route(request: AnalysisRequest, *, force_mode: Mode | None = None) -> AnalysisPlan:
    """질의 1건 -> 분석 계획 1개."""
    messages = [
        SystemMessage(content=prompts.SYSTEM),
        HumanMessage(content=_render_user(request)),
    ]

    try:
        plan = await llm_client.structured_call(
            messages,
            AnalysisPlan,
            profile="reasoning",
            component="engine_b.analysis_router",
        )
    except Exception as exc:
        log.warning("라우팅 LLM 실패 -> 키워드 폴백: %s", exc)
        plan = _fallback_plan(request)

    plan = _normalize(plan, request)
    if force_mode is not None:
        plan.mode = force_mode

    log.info(
        "route: mode=%s domains=%s focus=%s conf=%.2f",
        plan.mode,
        plan.domains,
        plan.focus,
        plan.confidence,
    )
    return plan


# ─── 프롬프트 조립 ────────────────────────────────────────────────

def _render_user(request: AnalysisRequest) -> str:
    settings = get_settings()
    ui = request.ui_context
    ui_text = ", ".join(
        f"{key}={value}"
        for key, value in (
            ("screen", ui.screen),
            ("project_id", ui.project_id),
            ("task_id", ui.task_id),
        )
        if value
    ) or "(없음)"

    history = ""
    if request.chat_history:
        recent = request.chat_history[-4:]
        lines = "\n".join(
            f"- {turn.get('role', 'user')}: {truncate(turn.get('content', ''), 150)}"
            for turn in recent
        )
        history = f"[직전 대화]\n{lines}\n"

    return prompts.USER_TEMPLATE.format(
        few_shots=prompts.build_few_shot_text(),
        query=request.query,
        ui_context=ui_text,
        requester=request.user_id,
        as_of=(request.as_of or settings.as_of()).isoformat(),
        history=history,
    )


# ─── 출력 보정 · 폴백 ─────────────────────────────────────────────

def _normalize(plan: AnalysisPlan, request: AnalysisRequest) -> AnalysisPlan:
    """LLM 출력의 흔한 어긋남을 코드로 바로잡는다."""
    # 알 수 없는 값 제거
    plan.domains = [d for d in dict.fromkeys(plan.domains) if d in KNOWN_DOMAINS]
    if not plan.domains:
        plan.domains = ["project"]
    plan.focus = [f for f in dict.fromkeys(plan.focus) if f in KNOWN_FOCUS]

    # 화면에서 프로젝트를 보고 있는데 대상이 비었으면 그 프로젝트로 본다
    if request.ui_context.project_id and not plan.entities.project_ids:
        plan.entities.project_ids = [request.ui_context.project_id]

    if not plan.objective:
        plan.objective = request.query

    plan.confidence = min(1.0, max(0.0, plan.confidence))
    return plan


def _fallback_plan(request: AnalysisRequest) -> AnalysisPlan:
    """LLM 을 못 쓰는 상황(키 없음/장애)에서도 그래프가 끝까지 돌게 하는 최소 라우팅."""
    query = request.query
    domains: list[str] = []

    if any(hint in query for hint in _MEETING_HINTS):
        domains.append("meeting")
    if any(hint in query for hint in _HCM_HINTS):
        domains.append("hcm")
    if any(hint in query for hint in _VACATION_HINTS):
        domains.append("vacation")
    if not domains or any(
        hint in query for hint in ("프로젝트", "일정", "예산", "진행", "마감", "위험")
    ):
        domains.insert(0, "project")

    mode: Mode = "replan" if any(hint in query for hint in _REPLAN_HINTS) else "analysis"

    return AnalysisPlan(
        mode=mode,
        domains=list(dict.fromkeys(domains)),  # type: ignore[arg-type]
        focus=[],
        objective=query,
        reasoning="LLM 라우팅에 실패해 키워드 기반으로 대체 라우팅했다.",
        confidence=0.3,
    )


# ─── Orchestrator 호환 진입점 ─────────────────────────────────────

async def run(domain: Domain, req) -> dict:
    """AgentRequest 를 받아 그래프를 끝까지 돌린다. 담당자 1의 orchestrator 가 부른다.

    domain 은 orchestrator 의 힌트일 뿐이고, 실제 도메인은 라우터가 다시 정한다.
    """
    from app.engine_b.graph import run_analysis

    state = await run_analysis(to_analysis_request(req))
    result = state.get("result")
    return {
        "domain": domain.value,
        "result": result.model_dump(mode="json") if result else None,
    }


def to_analysis_request(req) -> AnalysisRequest:
    """연동 규격 §3 의 AgentRequest -> Engine B 입력."""
    screen = getattr(req, "screenContext", None)
    form = getattr(screen, "formState", None) or {}
    return AnalysisRequest(
        query=req.goal,
        user_id=req.userId,
        chat_history=[
            {"role": m.role, "content": m.content} for m in (req.messages or [])
        ],
        ui_context=UIContext(
            screen=getattr(screen, "screen", None),
            project_id=form.get("projectId") or form.get("project_id"),
            task_id=form.get("taskId") or form.get("task_id"),
        ),
    )
