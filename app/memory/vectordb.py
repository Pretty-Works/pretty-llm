"""
문서형 저장소 — Chroma (회의록 본문 · 규정 문서)

  meetings 컬렉션  격리 축 = 프로젝트. 메타 {projectId, meetingId, ...}
                   검색은 반드시 "내가 속한 프로젝트" 필터와 함께 — 필터가 곧 권한.
                   (원본 접근은 어차피 meeting_detail 에서 BE 가 최종 검증 — 이중 방어)
  docs 컬렉션      격리 없음 = 전사 공용 (규정·매뉴얼)

★ Chroma 주의 (실측·명세 반영):
  · 동기 API — 이벤트 루프 블로킹 방지를 위해 전부 asyncio.to_thread 로 감싼다
  · 메타데이터는 스칼라(str·int·float·bool)만 — dict 를 넣으면 에러
  · 같은 ID 재색인은 delete → add (중복 방지 upsert)
"""

from __future__ import annotations

import asyncio

from app.memory.store import get_embeddings

_PERSIST_DIR = "data/chroma"

_collections: dict = {}


def _collection(name: str):
    if name not in _collections:
        from langchain_chroma import Chroma
        _collections[name] = Chroma(
            collection_name=name,
            embedding_function=get_embeddings(),   # store 쪽과 같은 모델 — 공간 일관
            persist_directory=_PERSIST_DIR,
        )
    return _collections[name]


def meetings_collection():
    return _collection("meetings")


def docs_collection():
    return _collection("docs")


# ── 회의록 ─────────────────────────────────────────────────
def _meeting_id(project_id: int, meeting_id: int) -> str:
    return f"{project_id}:{meeting_id}"


async def upsert_meeting(project_id: int, meeting_id: int, title: str,
                         meeting_date: str, purpose: str | None,
                         content: str | None, follow_up: str | None) -> None:
    vs = meetings_collection()
    doc_id = _meeting_id(project_id, meeting_id)
    text = (f"{title}\n목적: {purpose or '-'}\n내용: {content or '-'}\n"
            f"후속 조치: {follow_up or '-'}")
    meta = {"projectId": project_id, "meetingId": meeting_id,
            "title": title, "meetingDate": meeting_date}

    def _work():
        existing = vs.get(ids=[doc_id])
        if existing and existing.get("ids"):
            vs.delete(ids=[doc_id])
        vs.add_texts([text], metadatas=[meta], ids=[doc_id])

    await asyncio.to_thread(_work)


async def existing_meeting_ids(project_id: int) -> set[int]:
    """backfill 용 — 이 프로젝트에서 이미 색인된 meetingId 집합."""
    vs = meetings_collection()

    def _work():
        got = vs.get(where={"projectId": project_id})
        return {m["meetingId"] for m in (got.get("metadatas") or [])}

    return await asyncio.to_thread(_work)


async def search_meetings(query: str, project_ids: list[int], k: int = 3) -> list:
    """★ project_ids 가 비면 검색하지 않는다 — 필터 없는 검색은 남의 회의록 유출."""
    if not project_ids:
        return []
    vs = meetings_collection()
    return await asyncio.to_thread(
        vs.similarity_search, query, k, {"projectId": {"$in": project_ids}})


# ── 문서 (전사 공용) ────────────────────────────────────────
async def add_doc_chunks(filename: str, chunks: list[str]) -> None:
    vs = docs_collection()
    ids = [f"{filename}:{i}" for i in range(len(chunks))]
    metas = [{"source": filename, "chunk": i} for i in range(len(chunks))]

    def _work():
        existing = vs.get(where={"source": filename})
        if existing and existing.get("ids"):
            vs.delete(ids=existing["ids"])          # 같은 파일 재업로드 = 교체
        vs.add_texts(chunks, metadatas=metas, ids=ids)

    await asyncio.to_thread(_work)


async def search_docs(query: str, k: int = 4) -> list:
    vs = docs_collection()
    return await asyncio.to_thread(vs.similarity_search, query, k)


async def list_docs() -> dict[str, int]:
    """파일명 → 청크 수 (관리용)."""
    vs = docs_collection()

    def _work():
        got = vs.get()
        counts: dict[str, int] = {}
        for m in (got.get("metadatas") or []):
            counts[m["source"]] = counts.get(m["source"], 0) + 1
        return counts

    return await asyncio.to_thread(_work)
