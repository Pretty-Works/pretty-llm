# app/api/project.py
"""프로젝트 탭 AI 요약 생성 — 노션 「프로젝트 탭 상단 ai요약」 조회 명세의 생성측 짝.

POST /api/agent/project-summary  (Spring 배치 → FastAPI, 단발 JSON — SSE·내부도구 없음)
FE 가 보는 GET /api/v1/projects/{id}/summary?section= 은 BE 가 DB 에서 읽어 주는
조회 전용이고, 이 API 는 그 DB 에 저장할 요약 4종(overview·board·budget·meeting)을
한 번에 만들어 준다. 재료는 요청 바디로 전부 들어온다(meeting-draft 와 같은 방식).

응답은 A안(구조화 계약): headline + detail[] + stats[] — 담당자 1이 schemas 에
잡아둔 AiSummaryResult 초안과 같은 구조다. 칩(stats)과 D-day·건수·페이스 같은
숫자는 전부 코드가 계산하고, LLM 은 headline·detail 문장만 쓴다.
"""

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.common import llm_client
from app.config import get_settings
from app.prompts.ai_summary import SYSTEM
from app.schemas.response import StatItem

router = APIRouter()          # /api/v1/projects/** 자리 (routes.py 연결) — 현재 비어 있음
agent_router = APIRouter()    # /api/agent/** — main.py 가 prefix 없이 등록

SECTIONS = ("overview", "board", "budget", "meeting")


# ─── 요청/응답 (BE→LLM 규격: 봉투 없음, 실패는 HTTP 상태코드) ────

class SummaryRequest(BaseModel):
    """배치가 모아 보내는 재료. 항목별 필드는 기존 조회 명세의 result 형태를 그대로 따른다."""

    model_config = ConfigDict(extra="ignore")

    projectId: int
    today: str                                          # yyyy-MM-dd (Asia/Seoul)
    project: dict = Field(default_factory=dict)         # {name, startDate, targetDate}
    milestones: list[dict] = Field(default_factory=list)
    tasks: list[dict] = Field(default_factory=list)
    budget: dict = Field(default_factory=dict)
    expenses: list[dict] = Field(default_factory=list)
    posts: list[dict] = Field(default_factory=list)     # priority: HIGH | MID | LOW
    meetings: list[dict] = Field(default_factory=list)  # followUp 포함 요청
    upcomingMeetings: list[dict] = Field(default_factory=list)


class SectionSummary(BaseModel):
    section: str
    headline: str
    detail: list[str]
    stats: list[StatItem]


class SummaryResponse(BaseModel):
    projectId: int
    summaries: list[SectionSummary]


class _SectionDraft(BaseModel):
    headline: str
    detail: list[str] = Field(min_length=1, max_length=4)


class _Draft(BaseModel):
    """LLM 이 채우는 부분 — 문장만. section·stats 는 코드가 정한다."""

    overview: _SectionDraft
    board: _SectionDraft
    budget: _SectionDraft
    meeting: _SectionDraft


# ─── 섹션별 (사실 묶음, 칩) — 숫자는 전부 여기서 센다 ─────────────

def _won(amount: int) -> str:
    """11_400_000 → "₩11.4M" (배너 칩 표기)."""
    text = f"{amount / 1_000_000:.1f}".rstrip("0").rstrip(".")
    return f"₩{text}M"


def _dday(target: str | None, today: date) -> str:
    if not target:
        return ""
    try:
        delta = (date.fromisoformat(target[:10]) - today).days
    except ValueError:
        return ""
    return f" (D-{delta})" if delta >= 0 else f" (D+{-delta})"


def _facts_overview(req: SummaryRequest, today: date) -> tuple[str, list[StatItem]]:
    name = req.project.get("name", f"프로젝트 {req.projectId}")
    ms = req.milestones
    done = [m for m in ms if m.get("completed")]
    rate = round(len(done) / len(ms) * 100) if ms else 0
    next_ms = (next((m for m in ms if m.get("isNext")), None)
               or next((m for m in ms if not m.get("completed")), None))

    open_items = [t for t in req.tasks if not t.get("completed")]
    due = lambda t: t.get("dueDate") or "9999-12-31"
    overdue = [t for t in open_items if due(t) < today.isoformat()]
    carry = [t for t in open_items if t.get("isCarryOver")]
    soon_days = get_settings().due_soon_days
    soon_end = (today + timedelta(days=soon_days)).isoformat()
    due_soon = [t for t in open_items if today.isoformat() <= due(t) <= soon_end]

    fmt = lambda t: (f"\"{t.get('content')}\"({t.get('assigneeName')},"
                     f" 마감 {t.get('dueDate')}{_dday(t.get('dueDate'), today)})")
    lines = [
        f"[프로젝트] {name} — 목표일 {req.project.get('targetDate')}"
        f"{_dday(req.project.get('targetDate'), today)}",
        f"[마일스톤] 전체 {len(ms)}개 중 {len(done)}개 완료 ({rate}%)",
    ]
    if next_ms:
        lines.append(f"[다음 마일스톤] {next_ms.get('goal')} — 목표일"
                     f" {next_ms.get('targetDate')}{_dday(next_ms.get('targetDate'), today)}")
    lines.append(f"[할 일] 미완료 {len(open_items)}건 / 마감 임박({soon_days}일 내)"
                 f" {len(due_soon)}건 / 지연 {len(overdue)}건 / 지난주 이월 {len(carry)}건")
    for t in (overdue + carry)[:3]:
        lines.append(f"  - 밀린 항목: {fmt(t)}")
    for t in due_soon[:3]:
        lines.append(f"  - 임박 항목: {fmt(t)}")

    stats = [
        StatItem(label="진행률", value=f"{rate}%"),
        StatItem(label="임박 마감", value=f"{len(due_soon)}건"),
        StatItem(label="지연", value=f"{len(overdue)}건"),
    ]
    return "\n".join(lines), stats


def _facts_board(req: SummaryRequest) -> tuple[str, list[StatItem]]:
    posts = req.posts
    high = [p for p in posts if p.get("priority") == "HIGH"]
    mid = [p for p in posts if p.get("priority") in ("MID", "MEDIUM")]
    lines = [f"[게시판] 전체 {len(posts)}건 / HIGH {len(high)}건 / MID {len(mid)}건"]
    for p in high:
        lines.append(f"  - HIGH: \"{p.get('title')}\""
                     f" ({p.get('authorName')}·{p.get('department')}, {p.get('createdAt')})")
    for p in mid[:3]:
        lines.append(f"  - MID: \"{p.get('title')}\""
                     f" ({p.get('authorName')}·{p.get('department')}, {p.get('createdAt')})")

    stats = [
        StatItem(label="전체", value=f"{len(posts)}건"),
        StatItem(label="HIGH", value=f"{len(high)}건"),
        StatItem(label="MID", value=f"{len(mid)}건"),
    ]
    return "\n".join(lines), stats


def _facts_budget(req: SummaryRequest) -> tuple[str, list[StatItem]]:
    b = req.budget
    execution, elapsed = b.get("executionRate", 0), b.get("elapsedRate", 0)
    diff = execution - elapsed
    lines = [
        f"[예산] 전체 ₩{b.get('targetBudget', 0):,} 중 ₩{b.get('spentAmount', 0):,} 집행"
        f" — 집행률 {execution}%, 잔여 ₩{b.get('remainingAmount', 0):,}",
        f"[페이스] 기간 경과율 {elapsed}% 대비 집행률이 {abs(diff)}%p {'빠름' if diff >= 0 else '느림'}",
    ]
    top = max(b.get("byCategory", []), key=lambda c: c.get("share", 0), default=None)
    if b.get("byCategory"):
        lines.append("[비목 비중] " + " · ".join(
            f"{c.get('categoryLabel')} {c.get('share')}%" for c in b["byCategory"]))
    biggest = max(req.expenses, key=lambda e: e.get("amount", 0), default=None)
    if biggest:
        lines.append(f"[최대 지출] {biggest.get('purpose')} ₩{biggest.get('amount', 0):,}"
                     f" ({biggest.get('spenderName')}, {biggest.get('expenseDate')})")

    stats = [StatItem(label="집행률", value=f"{execution}%"),
             StatItem(label="잔여", value=_won(b.get("remainingAmount", 0)))]
    if top:
        stats.append(StatItem(label=top.get("categoryLabel", "최대 비목"),
                              value=f"{top.get('share', 0)}%"))
    return "\n".join(lines), stats


def _facts_meeting(req: SummaryRequest, today: date) -> tuple[str, list[StatItem]]:
    meetings = sorted(req.meetings, key=lambda m: m.get("meetingDate") or "", reverse=True)
    no_followup = [m for m in meetings if not (m.get("followUp") or "").strip()]
    lines = [f"[회의록] 기록 {len(meetings)}건, 후속 액션 미정리 {len(no_followup)}건"]
    for m in meetings[:3]:
        follow = (m.get("followUp") or "").strip() or "후속 액션 미정리"
        lines.append(f"  - {m.get('meetingDate')} \"{m.get('title')}\""
                     f" ({m.get('authorName')}) — {follow}")
    nxt = next((s for s in req.upcomingMeetings
                if (s.get("startAt") or "") >= today.isoformat()), None)
    lines.append("[다음 회의] " + (f"{nxt.get('startAt')} \"{nxt.get('title')}\" 등록됨"
                                  if nxt else "등록된 일정 없음"))

    stats = [
        StatItem(label="회의", value=f"{len(meetings)}건"),
        StatItem(label="후속 액션",
                 value=f"미정리 {len(no_followup)}건" if no_followup else "정리됨"),
        StatItem(label="다음 회의", value="등록됨" if nxt else "미등록"),
    ]
    return "\n".join(lines), stats


# ─── 엔드포인트 ───────────────────────────────────────────────────

@agent_router.post("/api/agent/project-summary", response_model=SummaryResponse)
async def project_summary(req: SummaryRequest) -> SummaryResponse:
    """4개 섹션 요약을 LLM 1콜로 생성한다. BE 는 결과를 DB 에 저장해 조회 API 로 서빙한다."""
    try:
        today = date.fromisoformat(req.today)
    except ValueError:
        raise HTTPException(status_code=422, detail="today 는 yyyy-MM-dd 형식이어야 합니다")

    collected = {
        "overview": _facts_overview(req, today),
        "board": _facts_board(req),
        "budget": _facts_budget(req),
        "meeting": _facts_meeting(req, today),
    }
    facts = "\n\n".join(f"## {s}\n{collected[s][0]}" for s in SECTIONS)

    try:
        draft = await llm_client.structured_call(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": f"기준일: {req.today}\n\n사실 묶음:\n{facts}"}],
            _Draft, profile="reasoning", component="project_summary",
        )
    except llm_client.LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))

    return SummaryResponse(
        projectId=req.projectId,
        summaries=[
            SectionSummary(section=s, headline=getattr(draft, s).headline,
                           detail=getattr(draft, s).detail, stats=collected[s][1])
            for s in SECTIONS
        ],
    )
