# app/clients/gmail_mcp_client.py
"""Agent(이 프로세스)가 Gmail MCP 서버의 툴을 가져다 쓰는 창구.

⑧ Agent는 토큰을 전혀 모른다 — 여기서 하는 일은 MCP 서버 URL에 붙어 tool 목록을
받아오는 것뿐이다. 인증/토큰/리프레시는 전부 mcp_servers/gmail_mcp/ 쪽 책임이고,
이 파일은 그 존재조차 모른다.

사용처: engine_a/domain_agents.py 같은 곳에서 다른 LangChain 툴들과 나란히
bind_tools() 에 섞어 쓴다. Gmail 관련 요청이 오면 LLM이 이 중 하나를 고른다.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_mcp_adapters.client import MultiServerMCPClient

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger("clients.gmail_mcp_client")


@lru_cache(maxsize=1)
def _client() -> MultiServerMCPClient:
    settings = get_settings()
    return MultiServerMCPClient(
        {
            "gmail": {
                # 끝의 슬래시 필수 — gmail-mcp 쪽 mount("/mcp", ...) + 내부 route("/") 조합이라
                # 슬래시 없이 치면 307 리다이렉트가 한 번 더 낀다.
                "url": f"{settings.gmail_mcp_server_url.rstrip('/')}/mcp/",
                "transport": "streamable_http",
            }
        }
    )


async def get_gmail_tools() -> list:
    """LangChain Tool 객체 리스트. gmail_search_emails / gmail_get_email /
    gmail_send_email / gmail_connection_status 가 여기 담겨 온다.

    MCP 서버가 꺼져 있어도 Agent 전체가 죽지 않게 예외를 삼키고 빈 리스트로 폴백한다
    — Gmail 연동은 부가 기능이라 필수 경로를 막으면 안 된다.
    """
    try:
        return await _client().get_tools(server_name="gmail")
    except Exception as exc:  # noqa: BLE001 — MCP 서버 다운 등 어떤 이유든 폴백
        log.warning("gmail MCP 서버 연결 실패, gmail 툴 없이 진행: %s", exc)
        return []
