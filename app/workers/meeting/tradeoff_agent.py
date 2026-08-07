"""
Meeting Tradeoff Agent — 회의 시간 추천의 최종 결합기 (담당자3, meeting 워커)

schedule_agent(스케줄 관점: 빈 슬롯 후보)와 project_fit_agent(프로젝트 관점: 적합 타이밍)의
출력을 받아, 후보 슬롯을 **1~N위 순위**로 추천한다.

    schedule_agent  ─(후보 슬롯)─┐
                                 ├─→ tradeoff_agent → 순위 추천
    project_fit_agent ─(적합도)─┘

두 축의 성격이 달라 결합 방식도 나눈다:
  · 참석 가능 인원 = 코드로 센다(그 시간에 일정 없는 참가자 수 = 셀 수 있는 값).
  · 최종 순위     = LLM 이 종합한다(참석 인원이 많다고 1순위가 아니라, 프로젝트
                    적합도가 그걸 뒤집을 수 있으므로). LLM 실패 시 참석 인원 순 폴백.

출력: MeetingTradeoffResult (render() 로 채팅용 텍스트도 만든다).
"""
from __future__ import annotations

import json
from datetime import datetime

import httpx
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.config import settings
from app.schemas.state import MeetingSlot, WorkerOutput
from app.workers.meeting.schedule_agent import fetch_schedules

llm = ChatOpenAI(model=settings.llm_model, api_key=settings.llm_api_key)


# ─── 결과 스키마 ──────────────────────────────────────────────────

class RankedSlot(BaseModel):
    rank: int                    # 1 = 1순위
    start: str
    end: str
    available_count: int         # 참석 가능 인원
    total_count: int             # 전체 참가자
    project_fit: str = ""        # 높음 / 중간 / 낮음
    reason: str = ""


class MeetingTradeoffResult(BaseModel):
    ranked: list[RankedSlot] = Field(default_factory=list)   # rank 오름차순
    reasoning: str = ""
    confidence: float = 0.7

    @property
    def recommended(self) -> RankedSlot | None:
        return self.ranked[0] if self.ranked else None


# ─── 참석 가능 인원 (코드로 카운트) ───────────────────────────────

def count_available(slot: MeetingSlot, participant_ids: list[int], schedules: list[dict]) -> int:
    """이 슬롯 시간에 '일정이 겹치지 않는' 참가자 수."""
    s, e = datetime.fromisoformat(slot.start), datetime.fromisoformat(slot.end)
    busy: set[int] = set()
    for sch in schedules:
        try:
            b_s = datetime.fromisoformat(sch["startAt"])
            b_e = datetime.fromisoformat(sch["endAt"])
        except (KeyError, ValueError):
            continue
        if not (e <= b_s or s >= b_e):           # 겹침
            uid = sch.get("userId")
            if uid is not None:
                busy.add(int(uid))
    return sum(1 for uid in participant_ids if uid not in busy)


# ─── LLM 종합 순위 ────────────────────────────────────────────────

_SYSTEM = """너는 회의 시간 추천의 최종 판단자다.
후보 슬롯마다 '참석 가능 인원'(스케줄 관점)과 '프로젝트 적합도'(프로젝트 관점)를 종합해
1순위부터 순위를 매긴다. 참석 인원이 많다고 무조건 1순위가 아니다 —
마감 임박·논의 시급성 같은 프로젝트 적합도가 더 중요하면 그게 앞선다.

반드시 아래 JSON 만 출력한다(설명 금지):
{
  "ranking": [
    {"index": <후보 번호 0부터>, "project_fit": "높음|중간|낮음", "reason": "한 줄 근거"}
  ],
  "reasoning": "종합 근거 한두 줄"
}
- ranking 은 순위 순서(맨 앞이 1순위)이며 모든 후보를 포함한다."""

_USER = """회의 목적: {purpose}
전체 참가자: {total}명

후보 슬롯:
{slots}

프로젝트 적합도 판단(project_fit 에이전트):
- 추천 슬롯: {fit_slot}
- 프로젝트 상황: {fit_status}
- 시급성: {fit_urgency}
- 근거: {fit_reasoning}"""


def _format_slots(slots: list[MeetingSlot], avail: list[int], total: int) -> str:
    return "\n".join(
        f"[{i}] {s.start} ~ {s.end} / 참석 가능 {avail[i]}/{total}명 ({s.reason})"
        for i, s in enumerate(slots)
    )


async def _rank_with_llm(slots, avail, total, fit_output, purpose) -> dict:
    fit = fit_output.result or {}
    rec = fit.get("recommended_slot") or {}
    user_msg = _USER.format(
        purpose=purpose,
        total=total,
        slots=_format_slots(slots, avail, total),
        fit_slot=f"{rec.get('start', '?')} ~ {rec.get('end', '?')}",
        fit_status=fit.get("project_status", ""),
        fit_urgency=fit.get("meeting_urgency", ""),
        fit_reasoning=fit_output.reasoning or "",
    )
    resp = await llm.ainvoke([
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_msg},
    ])
    return json.loads(resp.content)


def _valid(raw: dict, n: int) -> bool:
    ranking = raw.get("ranking")
    if not isinstance(ranking, list) or len(ranking) != n:
        return False
    seen = {item.get("index") for item in ranking}
    return seen == set(range(n))


# ─── 조립 / 폴백 ──────────────────────────────────────────────────

def _build(slots, avail, total, raw) -> list[RankedSlot]:
    out: list[RankedSlot] = []
    for rank, item in enumerate(raw["ranking"], start=1):
        i = item["index"]
        out.append(RankedSlot(
            rank=rank, start=slots[i].start, end=slots[i].end,
            available_count=avail[i], total_count=total,
            project_fit=item.get("project_fit", ""), reason=item.get("reason", ""),
        ))
    return out


def _fallback(slots, avail, total, fit_output) -> list[RankedSlot]:
    """LLM 실패 시: 참석 인원 많은 순. 적합도는 project_fit 추천 슬롯만 '높음'."""
    rec = (fit_output.result or {}).get("recommended_slot") or {}
    rec_start = rec.get("start")
    order = sorted(range(len(slots)), key=lambda i: avail[i], reverse=True)
    out: list[RankedSlot] = []
    for rank, i in enumerate(order, start=1):
        is_rec = slots[i].start == rec_start
        out.append(RankedSlot(
            rank=rank, start=slots[i].start, end=slots[i].end,
            available_count=avail[i], total_count=total,
            project_fit="높음" if is_rec else "중간",
            reason=slots[i].reason,
        ))
    return out


# ─── 메인 실행 ────────────────────────────────────────────────────

async def run(
    agent_input,                 # ScheduleAgentInput (participant_ids, from/to_date ...)
    schedule_output: WorkerOutput,
    fit_output: WorkerOutput,
    http: httpx.AsyncClient,
    purpose: str = "회의 일정 조율",
) -> MeetingTradeoffResult:
    """schedule + project_fit 출력을 결합해 순위 추천을 만든다."""
    slots = [MeetingSlot(**s) for s in (schedule_output.result or {}).get("slots", [])]
    participants = agent_input.participant_ids
    total = len(participants)

    if not slots:
        return MeetingTradeoffResult(reasoning="추천할 후보 슬롯이 없다.", confidence=0.0)

    # 참석 가능 인원 = 코드로 카운트
    schedules = await fetch_schedules(
        http, participants, agent_input.from_date, agent_input.to_date)
    avail = [count_available(s, participants, schedules) for s in slots]

    # 순위 = LLM 종합 (실패/무효 시 참석 인원 순 폴백)
    ranked: list[RankedSlot]
    reasoning = ""
    try:
        raw = await _rank_with_llm(slots, avail, total, fit_output, purpose)
        if _valid(raw, len(slots)):
            ranked = _build(slots, avail, total, raw)
            reasoning = raw.get("reasoning", "")
        else:
            ranked = _fallback(slots, avail, total, fit_output)
    except Exception:
        ranked = _fallback(slots, avail, total, fit_output)

    conf = round((float(schedule_output.confidence) + float(fit_output.confidence)) / 2, 2)
    return MeetingTradeoffResult(ranked=ranked, reasoning=reasoning, confidence=conf)


# ─── 채팅용 텍스트 렌더 ───────────────────────────────────────────

def _fmt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%m-%d %H:%M")
    except ValueError:
        return iso


def render(result: MeetingTradeoffResult) -> str:
    """추천 1순위/2순위/... 형태의 텍스트."""
    if not result.ranked:
        return "추천할 회의 시간을 찾지 못했어요."
    lines: list[str] = []
    for r in result.ranked:
        lines.append(f"추천 {r.rank}순위: {_fmt(r.start)} ~ {_fmt(r.end)}")
        lines.append(f"- 참석 가능: {r.available_count}명 (총 {r.total_count}명)")
        fit = r.project_fit + (f" — {r.reason}" if r.reason else "")
        lines.append(f"- 프로젝트 적합도: {fit}")
        lines.append("")
    return "\n".join(lines).rstrip()
