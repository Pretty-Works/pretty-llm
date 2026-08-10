# mcp_servers/gmail_mcp/mcp_tools.py
"""Agent에게 실제로 노출되는 MCP 툴들.

⑧ "Agent는 토큰을 전혀 모른다" — 이 파일의 모든 함수 시그니처를 보면 access_token/
refresh_token 이 인자로도 반환값으로도 등장하지 않는다. Agent는 user_id 만 넘긴다.

user_id 에 대한 메모 — app/tools/registry.py 의 RunContext 는 지금 run_id 만 갖고
userId 는 안 보낸다("사칭 경로가 생긴다"는 이유). Gmail 은 대화 1회성 run_id 가 아니라
"이 회사 계정 = 이 Gmail 계정" 처럼 장기로 묶여야 하는 연결이라 별도 user_id 가 필요하다.
운영 붙일 때 두 방식 중 하나를 고른다:
  (a) Agent 가 RunContext 에 안전한 채널로 받은 user_id 를 그대로 넘긴다
      (Spring이 X-Run-Id → userId 역산을 이미 하니, run 시작 시 한 번 받아 세션에 캐시)
  (b) 이 서버가 자체적으로 run_id 를 받아 Spring 내부 API로 역산해서 쓴다
      (Agent 프로세스는 여전히 user_id 도 token 도 모르게 유지된다)
지금은 (a) 를 가정하고 user_id 파라미터로 받는다.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from mcp_servers.gmail_mcp import gmail_api, token_resolver

mcp = FastMCP(
    name="gmail",
    instructions=(
        "회사 Gmail 계정을 검색/조회/발송한다. 호출 전 사용자가 Gmail 연결을 "
        "완료했어야 한다(미연결이면 not_connected 에러)."
    ),
    # FastMCP.streamable_http_app() 은 기본적으로 이 경로에 라우트를 하나 더 건다.
    # server.py 에서 app.mount("/mcp", ...) 로 이미 바깥쪽 경로를 잡아주고 있으므로,
    # 여기서는 "/" 로 둬야 최종 경로가 /mcp/mcp 가 아니라 /mcp 가 된다.
    streamable_http_path="/",
)


async def _resolve_token(user_id: str) -> str | None:
    try:
        return await token_resolver.get_valid_access_token(user_id)
    except token_resolver.NotConnected:
        return None


@mcp.tool()
async def gmail_search_emails(user_id: str, query: str, max_results: int = 10) -> dict:
    """Gmail 검색. query 는 Gmail 검색 문법 그대로 (예: "from:boss@company.com is:unread").

    반환: {"connected": bool, "messages": [...]}  — connected=false 면 먼저 연결부터 시켜야 함.
    """
    access_token = await _resolve_token(user_id)
    if access_token is None:
        return {"connected": False, "messages": []}
    messages = await gmail_api.search_messages(access_token, query, max_results)
    return {"connected": True, "messages": messages}


@mcp.tool()
async def gmail_get_email(user_id: str, message_id: str) -> dict:
    """검색으로 얻은 message_id 로 본문 전체를 가져온다."""
    access_token = await _resolve_token(user_id)
    if access_token is None:
        return {"connected": False}
    message = await gmail_api.get_message(access_token, message_id)
    return {"connected": True, "message": message}


@mcp.tool()
async def gmail_send_email(user_id: str, to: str, subject: str, body: str) -> dict:
    """메일 발송. WRITE 작업이므로 Agent 쪽에서 app/tools/registry.py 의 승인 흐름을 태워야 한다
    (auto 모드 자동 통과 대상이 아니다 — 상대방에게 실제로 메일이 나간다)."""
    access_token = await _resolve_token(user_id)
    if access_token is None:
        return {"connected": False}
    result = await gmail_api.send_message(access_token, to, subject, body)
    return {"connected": True, "messageId": result.get("id")}


@mcp.tool()
async def gmail_connection_status(user_id: str) -> dict:
    """Agent가 '메일 보내줘' 요청을 받았는데 미연결일 때, 사용자에게 연결부터 안내하려고 쓴다."""
    access_token = await _resolve_token(user_id)
    return {"connected": access_token is not None}
