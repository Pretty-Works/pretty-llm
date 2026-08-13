"""
카드 저장소 — AsyncSqliteStore (사용자 서랍: 대화 요약 · 분석 결과)

저장소를 둘로 나눈 설계(확정)의 "카드" 쪽이다:
  카드형 (작고 구조화, get→수정→put, 사용자 격리)  → 여기 (Store)
  문서형 (불변, 청크, 대량화 가능)                 → vectordb.py (Chroma)

서랍 주소(네임스페이스)가 곧 격리다:
  ("conv", uid)      대화 요약 카드 — key = conversationId (증분 확장 대상)
  ("analyses", uid)  분석 결과 카드 — key = uuid (누적)

임베딩 색인은 title·summary 필드로 고정 — 검색 질의와 의미적으로 맞닿을
텍스트만 색인한다 (ID·날짜류는 노이즈라 제외. 설계 결정, LLM이 정하지 않음).
"""

from __future__ import annotations

import asyncio
import aiosqlite
from pathlib import Path

from langgraph.store.sqlite import AsyncSqliteStore

from app.clients.backend import backend

_MEMORY_DB = "data/memories.sqlite"
CARD_CAP = 200                     # 서랍당 카드 상한 — 초과 시 오래된 것부터 정리

_embed = None
_store: AsyncSqliteStore | None = None
_user_cache: dict[str, int] = {}   # run_id → userId (run 수명 동안 유효)


def get_embeddings():
    """text-embedding-3-small 싱글톤 — Chroma 쪽(vectordb.py)도 이걸 재사용해야
    검색 공간이 일관된다."""
    global _embed
    if _embed is None:
        from langchain_openai import OpenAIEmbeddings
        _embed = OpenAIEmbeddings(model="text-embedding-3-small")
    return _embed


async def get_memory_store() -> AsyncSqliteStore:
    """싱글톤 + 지연 생성 (체크포인터와 같은 패턴)."""
    global _store
    if _store is None:
        path = Path(_MEMORY_DB)
        path.parent.mkdir(parents=True, exist_ok=True)
        # ★ isolation_level=None (autocommit) 필수 — AsyncSqliteStore 는 연산마다
        #   명시적 BEGIN 을 실행하는데(store/sqlite/aio.py:228), 기본 모드('')는
        #   setup() 후 트랜잭션을 열어둔 채라 그 BEGIN 이 "cannot start a transaction
        #   within a transaction" 으로 영구 실패한다. LangGraph 자신도
        #   from_conn_string 에서 같은 값을 쓴다(aio.py:140).
        #   ※ 체크포인터(AsyncSqliteSaver)는 BEGIN 을 안 써서 기본값으로도 멀쩡하다.
        conn = await aiosqlite.connect(path, isolation_level=None)
        # ★ 키 이름 주의 — sqlite 구현은 "fields" 가 아니라 "text_fields" 를 읽는다
        #   (store/sqlite/base.py:1410). "fields" 로 주면 조용히 무시되고 기본값
        #   ["$"](문서 전체)로 색인돼, 명세가 배제하려던 created·conversationId
        #   같은 노이즈까지 벡터에 섞인다. 범용 IndexConfig 와 이름이 다르므로
        #   호환을 위해 둘 다 넣어둔다.
        _store = AsyncSqliteStore(
            conn,
            index={"embed": get_embeddings(), "dims": 1536,
                   "text_fields": ["title", "summary"],
                   "fields": ["title", "summary"]},
        )
        if hasattr(_store, "setup"):
            await _store.setup()
    return _store


async def close_memory_store() -> None:
    """테스트·종료용. aiosqlite 는 비데몬 스레드라 안 닫으면 프로세스가 안 끝난다."""
    global _store
    if _store is not None:
        await _store.conn.close()
        _store = None


async def resolve_user_id(run_id: str) -> int:
    """신원 열쇠 — user.me(X-Run-Id 를 BE 가 역산)로만 userId 를 얻는다.

    ★ 신원 불변식: 이 함수 밖의 경로로 userId 를 구하지 말 것.
      LLM 인자·요청 바디의 신원은 신뢰하지 않는다 (사칭 차단).

    ★ 8/13 추가 — 대화 요약(summarize_run)이 done 직전에 이 함수를 부르는데,
      그 타이밍에 BE 가 run 을 이미 "끝난 것"으로 취급해 /me 를 4xx 로 거절하는
      사례가 실사용에서 발견됐다(대화 제목이 요약 대신 첫 질문으로 계속 폴백).
      원인은 BE 쪽 타이밍이라 근본 해결은 BE 협의가 필요하지만, 짧은 간격을 두고
      한 번만 다시 물어보면 통과하는 경우가 많아 완화책으로 재시도를 둔다.
    """
    if run_id not in _user_cache:
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                me = await backend.get("/me", run_id=run_id)
                _user_cache[run_id] = me["userId"]
                break
            except Exception as exc:                      # noqa: BLE001
                last_exc = exc
                if attempt == 0:
                    await asyncio.sleep(0.3)
        else:
            raise last_exc
    return _user_cache[run_id]


def _ns(namespace: tuple) -> tuple[str, ...]:
    """LangGraph 는 네임스페이스 라벨을 str 로만 받는다 — userId 는 int 라 변환한다.
    ("conv", 5) → ("conv", "5"). 호출부는 명세 표기대로 int 를 넘겨도 된다."""
    return tuple(str(p) for p in namespace)


async def put_card(namespace: tuple, key: str, value: dict) -> None:
    """카드 저장 + 서랍 상한 정리. value 에는 "created"(ISO 시각) 필수."""
    store = await get_memory_store()
    ns = _ns(namespace)
    await store.aput(ns, key, value)

    items = await store.asearch(ns, limit=CARD_CAP + 50)
    if len(items) > CARD_CAP:
        items.sort(key=lambda it: it.value.get("created", ""))
        for it in items[: len(items) - CARD_CAP]:
            await store.adelete(ns, it.key)


async def get_card(namespace: tuple, key: str) -> dict | None:
    store = await get_memory_store()
    item = await store.aget(_ns(namespace), key)
    return item.value if item else None


async def search_cards(namespace: tuple, query: str, limit: int = 3) -> list:
    """의미 검색 — 색인(title·summary) 기준. 반환: store Item 목록."""
    store = await get_memory_store()
    return await store.asearch(_ns(namespace), query=query, limit=limit)
