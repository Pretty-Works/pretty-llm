"""
Schedule Agent — 사람/스케줄 관점 회의 시간 추천 에이전트

흐름:
1. 참가자 캘린더 조회 (Tool)
2. 프로젝트 태스크 조회 (Tool)
3. 공통 빈 시간대 계산 (코드)
4. LLM: 빈 슬롯 + 프로젝트 맥락 → 후보 3개 추천
5. 결과 검증 후 AgentOutput 반환 (최대 2회 재시도)
"""

import json
from datetime import datetime, timedelta

import httpx
from langchain_openai import ChatOpenAI

from app.config import settings
from app.prompts.meeting_schedule import SCHEDULE_AGENT_SYSTEM, SCHEDULE_AGENT_USER
from app.schemas.state import AgentOutput, ScheduleAgentInput, ScheduleAgentResult, MeetingSlot

llm = ChatOpenAI(model=settings.openai_model, api_key=settings.openai_api_key)


# ── Tools ─────────────────────────────────────────────────────

async def fetch_schedules(
    http: httpx.AsyncClient,
    user_ids: list[int],
    from_date: str,
    to_date: str,
) -> list[dict]:
    """GET /api/v1/calendar/schedules — 참가자 일정 조회"""
    user_ids_str = ",".join(str(uid) for uid in user_ids)
    response = await http.get(
        "/api/v1/calendar/schedules",
        params={"from": from_date, "to": to_date, "userIds": user_ids_str},
    )
    response.raise_for_status()
    return response.json().get("result", [])


async def fetch_project_tasks(
    http: httpx.AsyncClient,
    project_id: int,
) -> list[dict]:
    """GET /api/v1/projects/{id}/tasks — 프로젝트 태스크 조회"""
    response = await http.get(f"/api/v1/projects/{project_id}/tasks")
    response.raise_for_status()
    return response.json().get("result", [])


# ── 공통 빈 슬롯 계산 (코드) ──────────────────────────────────

def find_free_slots(
    schedules: list[dict],
    from_date: str,
    to_date: str,
    duration_minutes: int,
) -> list[dict]:
    """
    참가자 전체 일정에서 모두 비어있는 시간대 계산.
    업무 시간(09:00~18:00), 평일만 대상.
    """
    # 바쁜 시간대 수집
    busy: list[tuple[datetime, datetime]] = []
    for schedule in schedules:
        start = datetime.fromisoformat(schedule["startAt"])
        end = datetime.fromisoformat(schedule["endAt"])
        busy.append((start, end))

    free_slots = []
    current = datetime.fromisoformat(f"{from_date}T09:00:00")
    end_boundary = datetime.fromisoformat(f"{to_date}T18:00:00")
    duration = timedelta(minutes=duration_minutes)

    while current + duration <= end_boundary:
        # 주말 건너뜀
        if current.weekday() >= 5:
            current = current.replace(hour=9, minute=0, second=0) + timedelta(days=1)
            continue

        # 업무 시간 초과 시 다음날로
        if current.hour >= 18:
            current = current.replace(hour=9, minute=0, second=0) + timedelta(days=1)
            continue

        slot_end = current + duration
        # 바쁜 시간과 겹치는지 확인
        overlap = any(
            not (slot_end <= b_start or current >= b_end)
            for b_start, b_end in busy
        )

        if not overlap:
            free_slots.append({
                "start": current.isoformat(),
                "end": slot_end.isoformat(),
            })

        current += timedelta(minutes=30)  # 30분 단위 탐색

    return free_slots


# ── LLM 추천 ──────────────────────────────────────────────────

def _format_schedules(schedules: list[dict]) -> str:
    if not schedules:
        return "없음"
    lines = []
    for s in schedules:
        lines.append(f"- {s.get('title', '일정')} ({s.get('startAt')} ~ {s.get('endAt')}) / 참가자: {s.get('userId')}")
    return "\n".join(lines)


def _format_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "없음"
    lines = []
    for t in tasks:
        lines.append(f"- [{t.get('status', '')}] {t.get('title', '')} / 마감: {t.get('dueDate', '없음')}")
    return "\n".join(lines)


def _format_free_slots(slots: list[dict]) -> str:
    if not slots:
        return "공통 빈 시간 없음"
    return "\n".join(f"- {s['start']} ~ {s['end']}" for s in slots[:20])  # 최대 20개만 전달


async def _call_llm(input_data: ScheduleAgentInput, schedules: list[dict], tasks: list[dict], free_slots: list[dict]) -> dict:
    user_msg = SCHEDULE_AGENT_USER.format(
        duration_minutes=input_data.duration_minutes,
        from_date=input_data.from_date,
        to_date=input_data.to_date,
        schedules=_format_schedules(schedules),
        tasks=_format_tasks(tasks),
        free_slots=_format_free_slots(free_slots),
    )

    response = await llm.ainvoke([
        {"role": "system", "content": SCHEDULE_AGENT_SYSTEM},
        {"role": "user", "content": user_msg},
    ])

    return json.loads(response.content)


# ── 검증 ──────────────────────────────────────────────────────

def _validate(raw: dict) -> bool:
    slots = raw.get("slots", [])
    if not slots or len(slots) < 1:
        return False
    for slot in slots:
        if not slot.get("start") or not slot.get("end") or not slot.get("reason"):
            return False
    return True


# ── 메인 실행 ─────────────────────────────────────────────────

async def run(input_data: ScheduleAgentInput, http: httpx.AsyncClient) -> AgentOutput:
    # Tool 호출
    schedules = await fetch_schedules(http, input_data.participant_ids, input_data.from_date, input_data.to_date)
    tasks = await fetch_project_tasks(http, input_data.project_id)

    # 공통 빈 슬롯 계산
    free_slots = find_free_slots(schedules, input_data.from_date, input_data.to_date, input_data.duration_minutes)

    # LLM 추천 (최대 2회 재시도)
    raw = {}
    for attempt in range(2):
        try:
            raw = await _call_llm(input_data, schedules, tasks, free_slots)
            if _validate(raw):
                break
        except Exception:
            if attempt == 1:
                raise

    slots = [MeetingSlot(**s) for s in raw.get("slots", [])[:3]]

    return AgentOutput(
        dimension="schedule",
        result=ScheduleAgentResult(slots=slots).model_dump(),
        reasoning=raw.get("reasoning", ""),
        confidence=float(raw.get("confidence", 0.7)),
    )
