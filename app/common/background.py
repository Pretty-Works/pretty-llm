"""
발사 후 망각(fire-and-forget) 작업의 공용 발사대

요약·색인처럼 "사용자 응답을 1ms 도 지연시키면 안 되는" 뒷일을 띄우는 곳이다.
호출부는 fire(코루틴) 한 줄만 쓰면 된다.

★ 왜 태스크를 집합에 붙들어 두는가
  asyncio 는 실행 중인 태스크를 **약한 참조로만** 들고 있다. create_task 의
  반환값을 아무도 안 잡으면 가비지 컬렉터가 실행 도중 수거할 수 있고, 그러면
  에러도 로그도 없이 조용히 사라진다 (asyncio 공식 문서가 경고하는 함정).
  집합에 넣고 끝날 때 빼면 그 구간 동안 강한 참조가 유지된다.
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine

# 실행 중인 뒷일들. 완료되면 스스로 빠진다 (누수 없음).
_TASKS: set[asyncio.Task] = set()


def fire(coro: Coroutine[Any, Any, Any]) -> None:
    """뒷일을 띄우고 잊는다. 결과도 예외도 호출부로 돌아오지 않는다.

    코루틴 쪽이 자기 예외를 스스로 삼키는 게 계약이다 (memory/*.py 는 전부
    try/except 로 감싸고 로그만 남긴다).
    """
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def pending_count() -> int:
    """아직 도는 뒷일 수 — 테스트·디버깅용."""
    return len(_TASKS)
