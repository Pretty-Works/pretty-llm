"""
Project Fit Agent — 프로젝트 관점 회의 타이밍 판단 에이전트

흐름:
1. 프로젝트 태스크/마일스톤 조회 (Tool)
2. LLM: schedule_agent가 추천한 슬롯 중 프로젝트 상황에 가장 맞는 타이밍 판단
3. 결과 검증 후 AgentOutput 반환 (최대 2회 재시도)
"""

import json

import httpx
from langchain_openai import ChatOpenAI

from app.config import settings
from app.prompts.meeting_project_fit import PROJECT_FIT_AGENT_SYSTEM, PROJECT_FIT_AGENT_USER
from app.schemas.state import AgentOutput, MeetingSlot

llm = ChatOpenAI(model=settings.llm_model, api_key=settings.llm_api_key)


# ── Tool ──────────────────────────────────────────────────────

async def fetch_project_tasks(http: httpx.AsyncClient, project_id: int) -> list[dict]:
    """GET /api/v1/projects/{id}/tasks — 프로젝트 태스크 조회"""
    response = await http.get(f"/api/v1/projects/{project_id}/tasks")
    response.raise_for_status()
    return response.json().get("result", [])


# ── 포맷 헬퍼 ─────────────────────────────────────────────────

def _format_slots(slots: list[MeetingSlot]) -> str:
    return "\n".join(
        f"- {s.start} ~ {s.end} ({s.reason})" for s in slots
    )


def _format_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "없음"
    lines = []
    for t in tasks:
        lines.append(
            f"- [{t.get('status', '')}] {t.get('title', '')} / 마감: {t.get('dueDate', '없음')}"
        )
    return "\n".join(lines)


# ── LLM 호출 ──────────────────────────────────────────────────

async def _call_llm(
    slots: list[MeetingSlot],
    tasks: list[dict],
    meeting_purpose: str,
) -> dict:
    user_msg = PROJECT_FIT_AGENT_USER.format(
        meeting_purpose=meeting_purpose,
        slots=_format_slots(slots),
        tasks=_format_tasks(tasks),
    )

    response = await llm.ainvoke([
        {"role": "system", "content": PROJECT_FIT_AGENT_SYSTEM},
        {"role": "user", "content": user_msg},
    ])

    return json.loads(response.content)


# ── 검증 ──────────────────────────────────────────────────────

def _validate(raw: dict) -> bool:
    slot = raw.get("recommended_slot", {})
    return bool(
        slot.get("start")
        and slot.get("end")
        and raw.get("reasoning")
        and raw.get("meeting_urgency")
    )


# ── 메인 실행 ─────────────────────────────────────────────────

async def run(
    project_id: int,
    slots: list[MeetingSlot],
    meeting_purpose: str,
    http: httpx.AsyncClient,
) -> AgentOutput:
    # Tool 호출
    tasks = await fetch_project_tasks(http, project_id)

    # LLM 판단 (최대 2회 재시도)
    raw = {}
    for attempt in range(2):
        try:
            raw = await _call_llm(slots, tasks, meeting_purpose)
            if _validate(raw):
                break
        except Exception:
            if attempt == 1:
                raise

    recommended = raw.get("recommended_slot", {})

    return AgentOutput(
        dimension="project_fit",
        result={
            "recommended_slot": recommended,
            "project_status": raw.get("project_status", ""),
            "meeting_urgency": raw.get("meeting_urgency", ""),
        },
        reasoning=raw.get("reasoning", ""),
        confidence=float(raw.get("confidence", 0.7)),
    )
