"""
색인 훅 — ③ 회의록 (Chroma) · ② 분석 결과 (Store 카드)

전부 발사 후 망각(asyncio.create_task 로 호출됨) — 예외를 밖으로 던지지 않는다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.clients.backend import backend
from app.memory import vectordb
from app.memory.store import put_card, resolve_user_id


async def index_meeting(run_id: str, project_id: int, meeting_id: int, title: str,
                        meeting_date: str, purpose: str | None,
                        content: str | None, follow_up: str | None) -> None:
    """meeting_create 성공 직후 — 본문이 이미 손에 있으므로 추가 조회 0회."""
    try:
        await vectordb.upsert_meeting(project_id, meeting_id, title, meeting_date,
                                      purpose, content, follow_up)
    except Exception as exc:                          # noqa: BLE001
        print(f"[memory] 회의록 색인 실패 (무시): {type(exc).__name__}: {exc}")


async def backfill_meetings(run_id: str, project_ids: list[int], limit: int = 5) -> None:
    """lazy 보충 색인 — 검색 직전 호출. 에이전트 밖(FE 화면)에서 만들어진 회의록의
    색인 공백을 메운다. 비용 상한: 총 limit 건."""
    try:
        filled = 0
        for pid in project_ids:
            if filled >= limit:
                break
            indexed = await vectordb.existing_meeting_ids(pid)
            listing = await backend.get(f"/projects/{pid}/meetings", run_id=run_id)
            for m in listing.get("meetings", []):
                if filled >= limit:
                    break
                if m["meetingId"] in indexed:
                    continue
                detail = await backend.get(
                    f"/projects/{pid}/meetings/{m['meetingId']}", run_id=run_id)
                await vectordb.upsert_meeting(
                    pid, detail["meetingId"], detail["title"], detail["meetingDate"],
                    detail.get("purpose"), detail.get("content"), detail.get("followUp"))
                filled += 1
    except Exception as exc:                          # noqa: BLE001
        print(f"[memory] 회의록 보충 색인 실패 (무시): {type(exc).__name__}: {exc}")


async def index_analysis(run_id: str, question: str, headline: str | None,
                         summary: str) -> None:
    """엔진 B result 시점 — 비싼 분석을 카드로 남겨 재활용한다."""
    try:
        uid = await resolve_user_id(run_id)
        await put_card(("analyses", uid), uuid.uuid4().hex, {
            "title": (headline or question)[:30],
            "summary": summary[:800],
            "question": question,
            "created": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:                          # noqa: BLE001
        print(f"[memory] 분석 색인 실패 (무시): {type(exc).__name__}: {exc}")
