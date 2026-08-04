"""
비동기 체크포인터 — 승인 대기 상태의 보관소

규격 v2 는 approval_request/question 에서 스트림을 닫고, BE 가 나중에
/resume 을 부르는 구조다. 두 HTTP 요청 사이에 에이전트의 멈춘 지점을
어딘가 보관해야 하는데 그게 checkpointer 다 (thread_id = runId).

vacation_agent 의 SqliteSaver(동기)와 달리 Async 판을 쓰는 이유:
  우리 도구가 전부 async(httpx AsyncClient)라 에이전트를 astream 으로
  돌려야 하는데, 동기 SqliteSaver 는 async 경로에서 NotImplementedError
  를 던진다. 같은 sqlite 파일을 쓰므로 데이터는 호환된다.
"""

from __future__ import annotations

import aiosqlite
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.config import settings

_saver: AsyncSqliteSaver | None = None


async def get_checkpointer() -> AsyncSqliteSaver:
    """싱글톤 + 지연 생성 (vacation_agent.get_agent 와 같은 이유)."""
    global _saver
    if _saver is None:
        path = Path(settings.checkpoint_db)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        _saver = AsyncSqliteSaver(conn)
        await _saver.setup()
    return _saver


async def close_checkpointer() -> None:
    """커넥션을 닫는다 (서버 shutdown·테스트 종료용).

    aiosqlite 는 비데몬 스레드를 하나 띄우는데, 닫지 않으면 그 스레드가
    살아남아 프로세스가 영영 종료되지 않는다.
    """
    global _saver
    if _saver is not None:
        await _saver.conn.close()
        _saver = None
