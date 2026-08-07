# app/api/integrations.py
"""'연동' 설정 화면(Gmail 등)이 붙는 라우터.

① 사용자 로그인 ~ ② "Gmail 연결하기" 클릭 구간을 담당한다. 이 라우터는 Gmail MCP
서버(mcp_servers/gmail_mcp)로 서버 투 서버 요청을 보내 authorize URL을 받아
프론트에 돌려줄 뿐이다 — code/token은 한 번도 이 프로세스를 거치지 않는다.

TODO(담당자1): user_id 추출은 프로젝트의 실제 인증 미들웨어가 붙으면 그걸로 교체.
지금은 다른 스텁 라우터들과 동일하게 자리만 잡아둔다.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from app.config import get_settings

router = APIRouter()


async def _gmail_mcp_client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=settings.gmail_mcp_server_url,
        headers={"X-Internal-Api-Key": settings.internal_api_key},
        timeout=10.0,
    )


@router.get("/gmail/connect-url")
async def gmail_connect_url(user_id: str) -> dict:
    """프론트의 'Gmail 연결하기' 버튼이 부르는 엔드포인트.

    반환된 authorize_url 로 프론트가 즉시 리다이렉트하면 ③(Google 동의화면)으로 넘어간다.
    """
    async with await _gmail_mcp_client() as client:
        resp = await client.post("/internal/gmail/connect-url", json={"user_id": user_id})
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="gmail-mcp 서버 응답 실패")
    return resp.json()


@router.get("/gmail/status")
async def gmail_status(user_id: str) -> dict:
    """설정 화면에 '연결됨(you@company.com)' / '연결 안됨' 뱃지를 그리기 위한 조회."""
    async with await _gmail_mcp_client() as client:
        resp = await client.get(f"/internal/gmail/status/{user_id}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="gmail-mcp 서버 응답 실패")
    return resp.json()


@router.delete("/gmail/connection")
async def gmail_disconnect(user_id: str) -> dict:
    async with await _gmail_mcp_client() as client:
        resp = await client.delete(f"/internal/gmail/connection/{user_id}")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="gmail-mcp 서버 응답 실패")
    return resp.json()
