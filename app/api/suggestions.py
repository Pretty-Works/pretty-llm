# app/api/suggestions.py
"""에이전트 패널 추천 문구 생성 — 화면을 열었을 때 보여줄 "먼저 해볼 것" 3개.

POST /api/agent/suggestions  (BE → FastAPI, 단발 JSON — SSE·내부도구 없음)
지금 FE 는 "회의록 작성해줘" 같은 고정 문구를 쓴다. 사용자마다 상황이 다른데
같은 문구를 보여주면 눌러도 되묻기부터 시작한다. 여기서는 사용자의 실제 상황
(밀린 할 일 · 후속 액션 미정리 회의 · 임박 마감 · 남은 연차)에서 후보를 골라
바로 실행 가능한 요청문까지 만들어 준다.

계약은 project-summary 와 같다 — 재료는 요청 바디로 전부 들어오고, 내부 도구를
호출하지 않으며, 실패는 HTTP 상태코드로 알린다.

★ 후보 선정은 코드가 한다. LLM 은 고른 후보를 문장으로 바꾸기만 한다.
  무엇을 추천할지까지 LLM 에 맡기면 "없는 회의"를 추천하는 걸 막을 방법이 없다.
"""

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.common import llm_client
from app.config import get_settings
from app.prompts.suggestions import SYSTEM

router = APIRouter()          # /api/v1 자리 — 현재 비어 있음
agent_router = APIRouter()    # /api/agent/** — main.py 가 prefix 없이 등록

MAX_SUGGESTIONS = 3           # 패널에 들어가는 칩 수


# ─── 요청/응답 ────────────────────────────────────────────────────

class SuggestionRequest(BaseModel):
    """재료. 필드명은 기존 조회 명세의 result 형태를 그대로 따른다."""

    model_config = ConfigDict(extra="ignore")

    today: str                                             # yyyy-MM-dd (Asia/Seoul)
    screen: str = "HOME"                                   # 화면 맥락 (HOME·PROJECT·CALENDAR…)
    projects: list[dict] = Field(default_factory=list)     # project.search result
    tasks: list[dict] = Field(default_factory=list)        # task.list result
    meetings: list[dict] = Field(default_factory=list)     # meeting.list + followUp
    upcomingMeetings: list[dict] = Field(default_factory=list)
    leaveBalance: dict = Field(default_factory=dict)       # leave.balance result
    recentQuestions: list[str] = Field(default_factory=list)  # 최근 대화 제목 — 중복 회피


class Suggestion(BaseModel):
    text: str                 # 사용자에게 보일 권유 문구
    prompt: str               # 눌렀을 때 에이전트로 보낼 요청문
    kind: str                 # 후보 종류 — FE 아이콘 매핑용


class SuggestionResponse(BaseModel):
    suggestions: list[Suggestion]


class _Draft(BaseModel):
    """LLM 이 채우는 부분 — 문장만. kind 는 코드가 정한다."""

    text: str
    prompt: str


class _Drafts(BaseModel):
    items: list[_Draft]


# ─── 후보 선정 (코드) ─────────────────────────────────────────────
#   (kind, 사실 묶음, 폴백 문구, 폴백 요청문) 순서대로 담는다.
#   앞에 있을수록 우선순위가 높다 — "지금 사용자를 곤란하게 하는 것" 순.

def _candidates(req: SuggestionRequest, today: date) -> list[dict]:
    out: list[dict] = []
    open_tasks = [t for t in req.tasks if not t.get("completed")]
    due = lambda t: t.get("dueDate") or "9999-12-31"

    # ① 지연된 할 일 — 가장 먼저 알려야 하는 것
    overdue = [t for t in open_tasks if due(t) < today.isoformat()]
    if overdue:
        names = ", ".join(f"\"{t.get('content')}\"" for t in overdue[:3])
        out.append({
            "kind": "overdue_task",
            "fact": f"마감이 지난 할 일 {len(overdue)}건: {names}",
            "text": f"밀린 할 일 {len(overdue)}건 정리해드릴까요?",
            "prompt": "마감이 지난 할 일을 정리하고 어떻게 처리할지 알려줘",
        })

    # ② 후속 액션이 안 적힌 회의 — 잊히는 지점
    loose = [m for m in req.meetings if not (m.get("followUp") or "").strip()]
    if loose:
        m = loose[0]
        out.append({
            "kind": "meeting_followup",
            "fact": f"후속 액션이 비어 있는 회의: \"{m.get('title')}\""
                    f" ({m.get('meetingDate')}, 프로젝트 {m.get('projectName') or '미상'})",
            "text": f"'{m.get('title')}' 회의 후속 액션 정리해드릴까요?",
            "prompt": f"{m.get('title')} 회의의 후속 액션을 정리해줘",
        })

    # ③ 마감 임박 — 이번 주에 손대야 하는 것
    soon_end = (today + timedelta(days=get_settings().due_soon_days)).isoformat()
    soon = [t for t in open_tasks if today.isoformat() <= due(t) <= soon_end]
    if soon:
        out.append({
            "kind": "due_soon",
            "fact": f"{get_settings().due_soon_days}일 안에 마감인 할 일 {len(soon)}건: "
                    + ", ".join(f"\"{t.get('content')}\"({t.get('dueDate')})" for t in soon[:3]),
            "text": f"이번 주 마감 {len(soon)}건 우선순위 잡아드릴까요?",
            "prompt": "이번 주 마감인 할 일들의 우선순위를 정리해줘",
        })

    # ④ 다가오는 회의 — 미리 준비할 것
    nxt = next((s for s in req.upcomingMeetings
                if (s.get("startAt") or "") >= today.isoformat()), None)
    if nxt:
        out.append({
            "kind": "upcoming_meeting",
            "fact": f"예정된 회의: \"{nxt.get('title')}\" ({nxt.get('startAt')})",
            "text": f"'{nxt.get('title')}' 회의록 미리 준비해드릴까요?",
            "prompt": f"{nxt.get('title')} 회의록 초안을 만들어줘",
        })

    # ⑤ 프로젝트 점검 — 위 신호가 없을 때의 기본 제안
    if req.projects:
        p = req.projects[0]
        out.append({
            "kind": "project_check",
            "fact": f"진행 중인 프로젝트: \"{p.get('name')}\""
                    f" (목표일 {p.get('targetDate') or '미정'})",
            "text": f"'{p.get('name')}' 위험 요소 점검해드릴까요?",
            "prompt": f"{p.get('name')} 프로젝트에 지금 위험한 게 있는지 분석해줘",
        })

    # ⑥ 남은 연차 — 여유가 있을 때
    remaining = req.leaveBalance.get("remainingDays")
    if remaining:
        out.append({
            "kind": "leave",
            "fact": f"올해 남은 연차 {remaining}일",
            "text": "쉬기 좋은 휴가 날짜 찾아드릴까요?",
            "prompt": "다음 달에 쉬기 좋은 날짜를 일정 보고 추천해줘",
        })

    # 최근에 이미 물어본 것과 겹치면 뺀다 — 같은 걸 또 권하지 않는다
    asked = " ".join(req.recentQuestions)
    fresh = [c for c in out if not _overlaps(c["text"], asked)]
    return (fresh or out)[:MAX_SUGGESTIONS]


def _overlaps(text: str, asked: str) -> bool:
    """추천 문구의 핵심어가 최근 질문에 이미 나왔는가 (따옴표 안 고유명사 기준)."""
    if not asked:
        return False
    quoted = [w.strip("'\"") for w in text.split("'") if len(w.strip("'\"")) > 3]
    return any(q and q in asked for q in quoted)


# ─── 엔드포인트 ───────────────────────────────────────────────────

@agent_router.post("/api/agent/suggestions", response_model=SuggestionResponse)
async def suggestions(req: SuggestionRequest) -> SuggestionResponse:
    """현재 상황에 맞는 추천 문구 3개. LLM 이 죽어도 코드 문구로 폴백한다."""
    try:
        today = date.fromisoformat(req.today)
    except ValueError:
        raise HTTPException(status_code=422, detail="today 는 yyyy-MM-dd 형식이어야 합니다")

    picked = _candidates(req, today)
    if not picked:
        return SuggestionResponse(suggestions=[])

    facts = "\n".join(f"{i + 1}. [{c['kind']}] {c['fact']}" for i, c in enumerate(picked))
    try:
        draft = await llm_client.structured_call(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": f"기준일: {req.today}\n화면: {req.screen}\n\n"
                                         f"후보 {len(picked)}개:\n{facts}"}],
            _Drafts, profile="worker", component="suggestions",
        )
        items = draft.items
    except Exception as exc:   # noqa: BLE001 — 추천은 부가 기능이라 패널을 비우지 않는다
        _log_fallback(exc)
        items = []

    return SuggestionResponse(suggestions=[
        Suggestion(
            text=(items[i].text if i < len(items) else picked[i]["text"])[:40],
            prompt=items[i].prompt if i < len(items) else picked[i]["prompt"],
            kind=picked[i]["kind"],
        )
        for i in range(len(picked))
    ])


def _log_fallback(exc: Exception) -> None:
    from app.utils.logger import get_logger

    get_logger("api.suggestions").warning(
        "추천 문구 생성 실패, 코드 문구로 폴백: %s: %s", type(exc).__name__, exc)
