"""
④ 문서 파이프라인 — txt 파일 → 검증 → 청킹 → docs 컬렉션 (전사 공용)

입구는 api/docs.py (POST /docs, 파일 그대로 업로드). 규정·매뉴얼처럼
모든 사원이 같은 답을 받아야 하는 문서가 대상이라 사용자 격리가 없다.
"""

from __future__ import annotations

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.memory import vectordb

_MAX_BYTES = 2 * 1024 * 1024        # 2MB

_splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)  # 교재 값


async def ingest_txt(filename: str, data: bytes) -> int:
    """검증 → 청킹 → 색인. 반환: 청크 수. 실패는 ValueError(한국어 메시지)."""
    if not filename.lower().endswith(".txt"):
        raise ValueError("txt 파일만 받습니다 (.txt 확장자)")
    if len(data) > _MAX_BYTES:
        raise ValueError("파일이 2MB 를 넘습니다")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("UTF-8 텍스트 파일이 아닙니다")
    if not text.strip():
        raise ValueError("빈 파일입니다")

    chunks = _splitter.split_text(text)
    await vectordb.add_doc_chunks(filename, chunks)
    return len(chunks)
