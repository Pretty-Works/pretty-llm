# app/api/integrations.py
"""'연동' 설정 화면(Gmail 등)이 붙는 라우터.

① 사용자 로그인 ~ ② "Gmail 연결하기" 클릭 구간을 담당한다. 이 라우터는 Gmail MCP
서버(mcp_servers/gmail_mcp)로 서버 투 서버 요청을 보내 authorize URL을 받아
프론트에 돌려줄 뿐이다 — code/token은 한 번도 이 프로세스를 거치지 않는다.

★ user_id 대신 run_id 를 받는다. Gmail 연결/상태조회/해제는 전부 Agent Chat
안에서만 일어난다는 전제라서(대화 1회 = run_id 1개), 프론트가 이미 갖고 있는
run_id 를 그대로 넘기면 된다. user_id 를 여기서 직접 받지 않는 이유:
  - 이 라우터가 실제 인증 미들웨어 없이 그냥 쿼리 파라미터를 믿는 스텁이라,
    user_id 를 그대로 받으면 아무나 남의 user_id 를 넣어 자기 구글 계정을
    남의 계정에 연결시킬 수 있었다(예전 구현의 구멍).
  - run_id 는 BE가 발급하고 BE만 user_id 로 되돌릴 수 있는 값이라, 여기서
    클라이언트가 뭘 보내든 "그 run 을 만든 사람"의 신원 이상으로는 못 간다.
  - user_id 로의 역산은 gmail-mcp 가 BE에 직접 물어서 한다
    (mcp_servers/gmail_mcp/run_resolver.py). 이 프로세스는 여전히 user_id 를
    모른 채로 유지된다.

이 라우터 자체를 진짜 인증으로 보호하는 문제는 app/common/auth.py의
verify_internal_api_key()로 처리한다 — 이 파일이 속한 app/api/routes.py
전체가 app/main.py에서 그 의존성을 걸고 include_router 되므로, 여기서 따로
챙길 코드는 없다. 다만 그 검증은 "요청이 진짜 BE에서 왔는가"까지만 보장하고
"그 run의 주인인가"는 여전히 run_id 자체(BE가 발급/역산)에 맡긴다 —
계층이 다른 문제라 하나가 다른 하나를 대신하지 않는다.
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


async def _call(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    """gmail-mcp 호출 공통 처리 — 응답 status_code 뿐 아니라 연결 자체가 실패하는
    경우(서버가 안 떠 있음/타임아웃 등)도 여기서 잡아 502로 통일한다. 예전엔
    status_code != 200 만 봤는데, httpx.ConnectError 같은 연결 단계 예외는 그
    체크에 안 걸리고 그대로 위로 튀어 올라가 500(스택트레이스 노출)로 새어
    나갔었다."""
    try:
        resp = await client.request(method, path, **kwargs)
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="gmail-mcp 서버 응답 실패")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="gmail-mcp 서버 응답 실패")
    return resp.json()


@router.get("/gmail/connect-url")
async def gmail_connect_url(run_id: str) -> dict:
    """프론트의 'Gmail 연결하기' 버튼이 부르는 엔드포인트.

    반환된 authorize_url 로 프론트가 즉시 리다이렉트하면 ③(Google 동의화면)으로 넘어간다.
    run_id → user_id 역산은 gmail-mcp 가 BE에 물어서 한다(이 프로세스는 관여 안 함).
    """
    async with await _gmail_mcp_client() as client:
        return await _call(client, "POST", "/internal/gmail/connect-url",
                           json={"run_id": run_id})


@router.get("/gmail/status")
async def gmail_status(run_id: str) -> dict:
    """설정 화면에 '연결됨(you@company.com)' / '연결 안됨' 뱃지를 그리기 위한 조회."""
    async with await _gmail_mcp_client() as client:
        return await _call(client, "GET", f"/internal/gmail/status/{run_id}")


@router.delete("/gmail/connection")
async def gmail_disconnect(run_id: str) -> dict:
    async with await _gmail_mcp_client() as client:
        return await _call(client, "DELETE", f"/internal/gmail/connection/{run_id}")
