"""
① 대화 요약 파이프라인 — done 시점에 발사되는 백그라운드 작업 (증분)

교재 06-Conversation-Summary 의 증분 패턴: 같은 대화(conversationId)면 카드를
새로 만들지 않고 기존 요약을 "확장"한다 — 대화당 카드 1장 유지.

★ 발사 후 망각 계약: 이 함수는 어떤 예외도 밖으로 던지지 않는다.
  (done 응답은 이미 나갔다 — 여기서 죽으면 로그 한 줄이 전부여야 한다)
"""

from __future__ import annotations

from datetime import datetime, timezone

from langchain.chat_models import init_chat_model
from pydantic import BaseModel, Field

from app.config import settings
from app.memory.store import get_card, put_card, resolve_user_id

_MIN_CONTENT = 60          # goal+answer 가 이보다 짧으면 기억할 가치 없음 → 스킵
_MAX_SUMMARY = 800         # 증분 확장의 무한 증식 방지 (교재 경고)


class ConvCard(BaseModel):
    title: str = Field(description="대화의 명사형 제목, 30자 이내 (예: '8월 연차 신청')")
    summary: str = Field(description="2~5문장. 수치·날짜·결정사항·ID 를 보존. 800자 이내")


_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
        _llm = model.with_structured_output(ConvCard)
    return _llm


async def summarize_run(run_id: str, conversation_id: int | None,
                        goal: str, answer: str) -> None:
    try:
        if conversation_id is None:
            return
        if len((goal or "") + (answer or "")) < _MIN_CONTENT:
            return                                   # 한 줄짜리 조회 — 저장 가치 없음

        uid = await resolve_user_id(run_id)
        key = str(conversation_id)
        existing = await get_card(("conv", uid), key)

        if existing:                                 # 증분 확장 (교재 패턴)
            prompt = (f"기존 대화 요약:\n{existing['summary']}\n\n"
                      f"이 대화에 새로 오간 내용:\n사용자 요청: {goal}\n"
                      f"에이전트 답변: {answer}\n\n"
                      f"기존 요약을 새 내용을 반영해 확장하라. "
                      f"{_MAX_SUMMARY}자를 넘으면 덜 중요한 부분을 줄여 압축하라. "
                      f"수치·날짜·결정사항은 반드시 보존하라.")
        else:
            prompt = (f"아래 대화를 요약하라. 수치·날짜·결정사항·생성된 ID 를 보존하라.\n"
                      f"사용자 요청: {goal}\n에이전트 답변: {answer}")

        card: ConvCard = await _get_llm().ainvoke(prompt)

        await put_card(("conv", uid), key, {
            "title": card.title[:30],
            "summary": card.summary[:_MAX_SUMMARY],
            "conversationId": conversation_id,
            "created": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:                          # noqa: BLE001 — 발사 후 망각 계약
        print(f"[memory] 대화 요약 실패 (무시): {type(exc).__name__}: {exc}")
