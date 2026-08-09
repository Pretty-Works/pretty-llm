# app/workers/meeting/adapter.py
"""담당자 3의 meeting 에이전트를 Engine B 워커로 붙이는 어댑터.

그쪽 에이전트는 프롬프트 선언형이 아니라 자체 구현이고, 슬롯 탐색 결과를
적합도 판단이 다시 받아 쓰는 순차 구조다. 그래서 노드 하나가 두 축을 내놓는다.

백엔드를 직접 호출하므로(픽스처 폴백 없음) 백엔드가 없으면 두 축 모두
error 로 내려간다. 그래프는 워커 실패를 흡수하므로 나머지 도메인 분석은 계속된다.
"""

import httpx
from pydantic import BaseModel

from app.config import get_settings
from app.schemas.state import (
    AnalysisContext,
    AnalysisPlan,
    MeetingSlot,
    ScheduleAgentInput,
    WorkerOutput,
    WorkerPayload,
)
from app.utils.logger import get_logger
from app.workers.base import WorkerSpec
from app.workers.meeting import project_fit_agent, schedule_agent, tradeoff_agent

log = get_logger("workers.meeting")

DEFAULT_DURATION_MINUTES = 60


class _MeetingResult(BaseModel):
    """WorkerSpec 계약을 채우기 위한 자리표시자. 실제 result 는 에이전트가 만든다."""


def _failed(dimension: str, reason: str) -> WorkerOutput:
    return WorkerOutput(
        dimension=dimension,
        domain="meeting",
        reasoning=f"회의 {dimension} 분석에 실패했다: {reason}",
        confidence=0.0,
        error=reason,
    )


def _schedule_input(plan: AnalysisPlan, context: AnalysisContext) -> ScheduleAgentInput | None:
    project = context.project(plan.entities.project_ids[0] if plan.entities.project_ids else None)
    if project is None or not (context.window_from and context.window_to):
        return None
    participants = [m.user_id for m in project.members] or [c.user_id for c in context.candidates]
    return ScheduleAgentInput(
        participant_ids=participants,
        project_id=project.id,
        duration_minutes=DEFAULT_DURATION_MINUTES,
        from_date=context.window_from.isoformat(),
        to_date=context.window_to.isoformat(),
    )


async def run_meeting(spec: WorkerSpec, payload: WorkerPayload) -> list[WorkerOutput]:
    """슬롯 탐색 -> 적합도 판단 순으로 돌리고 축 2개를 돌려준다."""
    plan: AnalysisPlan = payload["plan"]
    context: AnalysisContext = payload["context"]

    agent_input = _schedule_input(plan, context)
    if agent_input is None:
        reason = "대상 프로젝트나 분석 기간을 특정하지 못했다"
        return [_failed("schedule", reason), _failed("project_fit", reason)]

    settings = get_settings()
    timeout = httpx.Timeout(
        connect=settings.backend_connect_timeout_s, read=settings.backend_read_timeout_s,
        write=settings.backend_read_timeout_s, pool=settings.backend_connect_timeout_s,
    )

    try:
        async with httpx.AsyncClient(base_url=settings.backend_base_url, timeout=timeout) as http:
            scheduled = await schedule_agent.run(agent_input, http)
            slots = [MeetingSlot(**s) for s in (scheduled.result or {}).get("slots", [])]
            fit = await project_fit_agent.run(
                agent_input.project_id, slots, plan.objective or "회의 일정 조율", http
            )
            tradeoff = await tradeoff_agent.run(agent_input,scheduled,fit,http,purpose=plan.objective or "회의 일정 조율",)

            tradeoff_output = WorkerOutput(
                                            dimension="tradeoff",
                                            domain="meeting",
                                            result=tradeoff.model_dump(),
                                            reasoning=tradeoff.reasoning,
                                            confidence=tradeoff.confidence,)
            
    except Exception as exc:
        log.warning("meeting 에이전트 실패: %s", exc)
        return [_failed("schedule", str(exc)), _failed("project_fit", str(exc))]

    # 담당자 3 에이전트는 domain 을 채우지 않는다. 통합 단계 표기를 위해 붙여준다.
    return [
        scheduled.model_copy(update={"domain": "meeting"}),
        fit.model_copy(update={"domain": "meeting"}),
        tradeoff_output,
    ]


SPEC = WorkerSpec(
    domain="meeting",
    dimension="schedule",
    role="",
    method="",
    result_model=_MeetingResult,
    runner=run_meeting,
)
