# mcp_servers/gmail_mcp/server.py
"""Gmail MCP 서버 진입점. Agent(app/)와는 별도 프로세스/별도 배포로 띄운다.

실행:  uvicorn mcp_servers.gmail_mcp.server:app --port 8100
       (또는  python -m mcp_servers.gmail_mcp.server)

이 프로세스가 노출하는 것:
  - OAuth HTTP 엔드포인트 (oauth_routes.py)   : 사람이 브라우저로 접근
  - MCP 엔드포인트 /mcp (mcp_tools.py)         : Agent가 langchain-mcp-adapters로 접근
같은 프로세스에서 같이 뜨지만, 배포 시 이 서비스만 인터넷에 노출되고 Agent와는
사설망/내부 URL로만 통신하게 구성하는 걸 권장한다 (토큰이 도는 곳이니까).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from mcp_servers.gmail_mcp.config import get_settings
from mcp_servers.gmail_mcp.mcp_tools import mcp
from mcp_servers.gmail_mcp.oauth_routes import router as oauth_router

# FastMCP의 streamable-http ASGI 서브앱. 세션 매니저의 lifespan을 우리 앱 lifespan에
# 엮어줘야 tool 호출 시 "Task group is not initialized" 류 에러가 안 난다.
_mcp_asgi_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.validate_required()
    async with _mcp_asgi_app.router.lifespan_context(app):
        print(f"[gmail-mcp] listening — oauth + /mcp on port {settings.mcp_server_port}")
        yield


app = FastAPI(title="gmail-mcp", version="0.1.0", lifespan=lifespan)
app.include_router(oauth_router)
app.mount("/mcp", _mcp_asgi_app)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "gmail-mcp"}


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "mcp_servers.gmail_mcp.server:app",
        host=settings.mcp_server_host,
        port=settings.mcp_server_port,
    )
