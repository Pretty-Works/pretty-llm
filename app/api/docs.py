"""
문서 업로드 API — txt 파일을 받아 규정 RAG 에 넣는 수신구

규격(v2) 밖의 별도 문이다 — 채팅 계약과 무관하게 파일을 넣는 관리용 입구.
(FE 채팅에 파일 첨부가 생기면 BE 경유 통로로 승격 — 그때도 ingest_txt 재사용)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile

from app.memory import vectordb
from app.memory.docs_ingest import ingest_txt

router = APIRouter(prefix="/docs", tags=["docs"])


@router.post("")
async def upload_doc(file: UploadFile):
    data = await file.read()
    try:
        chunks = await ingest_txt(file.filename or "unnamed.txt", data)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    return {"filename": file.filename, "chunks": chunks}


@router.get("")
async def list_docs():
    return {"documents": await vectordb.list_docs()}
