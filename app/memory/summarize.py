"""
① 대화 요약 파이프라인 — done 시점에 도는 요약 작업 (증분)

교재 06-Conversation-Summary 의 증분 패턴: 같은 대화(conversationId)면 카드를
새로 만들지 않고 기존 요약을 "확장"한다 — 대화당 카드 1장 유지.

★ 8/12 변경 — "발사 후 망각"에서 "await 후 title 반환"으로 바꿨다.
  전에는 done 을 먼저 내보내고 이 함수를 fire()(백그라운드)로 띄워 recall용
  메모리 카드만 채웠다. 이제 BE 의 대화 목록 API(GET /agent/conversations)가
  title 필드로 "에이전트 요약 또는 첫 질문 앞부분"을 기대하므로, 그 title 을
  done 응답 바디에 실어 보내야 한다 — 그러려면 done 을 내보내기 "전에" 이
  함수가 끝나 있어야 한다(fire-and-forget으론 값을 done 에 실을 수 없다).
  그래서 호출부(app/common/hitl.py, app/orchestrator/composite.py)는 이제
  fire() 대신 await 로 부르고, 반환된 title 을 done payload 에 얹는다.

  대가: done 이 나가기 전에 LLM 호출 1번(요약 생성)만큼 지연이 늘어난다.
  다만 그 LLM 호출은 원래도 하던 일이라 "언제 하느냐"만 바뀐 것이고, 지연은
  보통 1~2초 내다 — 채팅 목록 제목이 정확해지는 대가로 감수하기로 했다.

★ 예외는 여전히 밖으로 던지지 않는다 — 실패해도 None 을 돌려줄 뿐이고
  (BE 가 title 누락 시 첫 질문으로 폴백하므로 안전), done 자체는 막지 않는다.
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
                        goal: str, answer: str) -> str | None:
    """대화를 요약해 recall용 카드로 저장하고, 채팅 목록 제목으로 쓸 title 을 돌려준다.

    실패하거나 스킵되면 None — 호출부는 이때 done payload 에 title 을 안 실으면
    된다(BE 가 첫 질문으로 알아서 폴백한다).
    """
    try:
        if conversation_id is None:
            return None
        if len((goal or "") + (answer or "")) < _MIN_CONTENT:
            return None                                # 한 줄짜리 조회 — 저장 가치 없음

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
        title = card.title[:30]

        await put_card(("conv", uid), key, {
            "title": title,
            "summary": card.summary[:_MAX_SUMMARY],
            "conversationId": conversation_id,
            "created": datetime.now(timezone.utc).isoformat(),
        })
        return title
    except Exception as exc:                          # noqa: BLE001 — 실패해도 done 은 막지 않는다
        print(f"[memory] 대화 요약 실패 (무시): {type(exc).__name__}: {exc}")
        return None
