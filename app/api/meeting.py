# app/api/meeting.py
"""회의록 초안 생성 — 노션 「(BE→LLM) 회의록 초안 생성」 구현.

POST /api/agent/meeting-draft  (Spring → FastAPI, 단발 JSON — SSE·내부도구 없음)
회의록 작성 화면(W_mnrec)에서 사용자가 txt 파일을 올리고 등록을 누르면, BE 가
파일에서 추출한 전문을 body 에 실어 호출한다. 화면 필드(회의명·일시·장소·목적·
주요 내용·후속 조치·참석자)를 뽑아 돌려주면 FE 가 폼에 채운다.
근거 없는 필드는 null — 지어내는 순간 결재 문서가 오염된다 (명세 §4).

기존 summarize·extract-followup 은 제거 (2026-08-07): BE 를 FE용 경로로 재조회하는
구조라 규격(Run 밖 내부도구 호출 금지)과 맞지 않았다. 필요해지면 이 방식(재료
body 수신)으로 다시 만든다.
"""

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.common import llm_client
from app.prompts.meeting_draft import SYSTEM

router = APIRouter()          # /api/v1/meetings 자리 (routes.py 연결) — 현재 비어 있음
agent_router = APIRouter()    # /api/agent/** — main.py 가 prefix 없이 등록

_LIMITS = {"title": 200, "location": 100, "purpose": 500}


# ─── 요청/응답 (BE→LLM 규격: 봉투 없음, 실패는 HTTP 상태코드) ────

class Member(BaseModel):
    model_config = ConfigDict(extra="ignore")

    userId: int
    name: str
    department: str | None = None
    position: str | None = None


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transcript: str                                   # txt 전문 (30,000자 이하 — BE 검증 후 전달)
    today: str                                        # yyyy-MM-dd. 상대 날짜 환산 기준
    projectMembers: list[Member] = Field(default_factory=list)   # 참석자 매칭의 유일한 소스


class MeetingDraft(BaseModel):
    """응답 = 회의록 작성 화면 필드. 근거 없으면 null (명세 §3)."""

    title: str | None = None
    meetingDate: str | None = None
    location: str | None = None
    purpose: str | None = None
    content: str | None = None
    followUp: str | None = None
    attendeeUserIds: list[int] = Field(default_factory=list)


# ─── 초안 추출 코어 — API 와 채팅 첨부(FILL_FORM) 도구가 같이 쓴다 ─

async def generate_draft(transcript: str, today: str, members: list[dict]) -> MeetingDraft:
    """txt 전문 → 초안. LLM 1콜 + 코드 후처리(명단 검증·길이 절단·날짜 형식)."""
    roster = "\n".join(
        f"- userId={m.get('userId')} {m.get('name')}"
        f" ({m.get('department') or '부서 미상'} · {m.get('position') or '직급 미상'})"
        for m in members
    ) or "(명단 없음 — attendeeUserIds 는 빈 배열로 둘 것)"

    draft = await llm_client.structured_call(
        [{"role": "system", "content": SYSTEM},
         {"role": "user", "content": f"기준일(today): {today}\n\n"
                                     f"프로젝트 참여자 명단:\n{roster}\n\n"
                                     f"회의 기록 전문:\n{transcript}"}],
        MeetingDraft, profile="reasoning", component="meeting_draft",
    )
    return _sanitize(draft, {m.get("userId") for m in members})


# ─── 엔드포인트 ───────────────────────────────────────────────────

@agent_router.post("/api/agent/meeting-draft", response_model=MeetingDraft)
async def meeting_draft(req: DraftRequest) -> MeetingDraft:
    try:
        date.fromisoformat(req.today)
    except ValueError:
        raise HTTPException(status_code=422, detail="today 는 yyyy-MM-dd 형식이어야 합니다")

    try:
        return await generate_draft(req.transcript, req.today,
                                    [m.model_dump() for m in req.projectMembers])
    except llm_client.LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))


def _sanitize(draft: MeetingDraft, roster_ids: set[int]) -> MeetingDraft:
    """명세의 안전선은 코드가 지킨다 — 명단 밖 참석자·중복 제거, 길이 절단, 날짜 검증."""
    draft.attendeeUserIds = [i for i in dict.fromkeys(draft.attendeeUserIds) if i in roster_ids]
    for field, limit in _LIMITS.items():
        value = getattr(draft, field)
        if value and len(value) > limit:
            setattr(draft, field, value[:limit])
    if draft.meetingDate:
        try:
            date.fromisoformat(draft.meetingDate)
        except ValueError:
            draft.meetingDate = None
    return draft
