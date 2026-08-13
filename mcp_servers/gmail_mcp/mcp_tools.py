# mcp_servers/gmail_mcp/mcp_tools.py
"""Agent에게 실제로 노출되는 MCP 툴들.

⑧ "Agent는 토큰도 user_id도 모른다" — 이 파일의 모든 함수 시그니처를 보면
access_token/refresh_token/user_id 가 인자로도 반환값으로도 등장하지 않는다.
Agent(LLM)는 run_id 만 넘기고, user_id 로의 역산은 이 서버가 run_resolver.py를
통해 Spring BE에 직접 물어서 한다(§ docs/gmail_mcp_oauth.md 참고, (b) 방식 채택).

run_id 를 여기서 직접 받는 이유 — app/tools/registry.py 의 RunContext 는 지금도
run_id 만 갖고 userId 는 안 보낸다("사칭 경로가 생긴다"는 이유). 그 원칙을 Gmail
쪽에서도 그대로 지키려면, 이 tool 함수들도 user_id 가 아니라 run_id 를 받아야
한다 — Agent 쪽(app/clients/gmail_mcp_client.py)이 tool 스키마에서 run_id 를
LLM에게 숨기고 RunContext 의 현재 run_id 로 강제 주입해주는 것과 짝을 이룬다.
LLM 이 임의의 user_id 를 tool_call.args 에 써 보낼 수 있는 경로 자체가 없다.

BE의 run_id→user_id API 가 아직 준비 중이라, 지금은 run_resolver.py 의
dev_run_id_passthrough 로 로컬 테스트를 돌릴 수 있다. API 나오면 run_resolver.py
만 손보면 되고 이 파일은 그대로 둔다.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mcp_servers.gmail_mcp import gmail_api, google_oauth, run_resolver, state_token, token_resolver
from mcp_servers.gmail_mcp.config import get_settings
from mcp_servers.gmail_mcp.logger import get_logger

log = get_logger("mcp_tools")

# ★ 2026-08-13 — FastMCP(...)에 host= 를 안 넘기면 SDK가 기본값 "127.0.0.1"로
#   판단해 DNS-rebinding 방지용 Host 헤더 허용 목록을 자동으로
#   ["127.0.0.1:*","localhost:*","[::1]:*"] 로만 좁혀버린다(mcp/server/fastmcp/
#   server.py). Docker Compose 내부망에서 Agent가 이 서버를 "gmail-mcp:8100"
#   Host 헤더로 부르니 항상 421 Invalid Host header 로 막혔다 — 실제 배포
#   호스트명을 config.py(GMAIL_MCP_ALLOWED_HOSTS)에서 읽어와 명시적으로 허용한다.
_allowed_hosts = [h.strip() for h in get_settings().mcp_allowed_hosts.split(",") if h.strip()]
_transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_allowed_hosts,
    allowed_origins=[f"{scheme}://{h}" for h in _allowed_hosts for scheme in ("http", "https")],
)

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
    transport_security=_transport_security,
)


async def _resolve_token(run_id: str) -> str | None:
    """run_id → user_id → access_token. 둘 중 어느 단계에서 실패하든 None(미연결 취급)."""
    try:
        user_id = await run_resolver.resolve_user_id(run_id)
    except run_resolver.RunResolutionError as exc:
        log.warning("run_id=%s → user_id 조회 실패: %s", run_id, exc)
        return None

    try:
        return await token_resolver.get_valid_access_token(user_id)
    except token_resolver.NotConnected:
        return None


@mcp.tool()
async def gmail_search_emails(
    run_id: str,
    query: str,
    max_results: int = 10,
) -> dict:
    """Gmail 검색. 대화에서 언급된 조건만 아래 문법으로 옮겨 담는다(지어내지 말 것).
    여러 조건은 공백으로 이어 쓰면 AND 로 합쳐진다.

      from:이름/이메일   보낸 사람       is:unread / is:read   읽음 상태
      to:이름/이메일     받는 사람       has:attachment         첨부파일 여부
      subject:키워드     제목 검색       after:YYYY/MM/DD       이 날짜 이후
                                         before:YYYY/MM/DD      이 날짜 이전

    조건이 하나도 없는 요청("가장 최근 메일 뭐야?" 등)은 query="" (빈 문자열)로
    부르면 메일함 전체에서(라벨 제한 없음 — 보낸 메일함 포함) 검색된다. 이런
    요청에 발신자를 되물을 필요가 없다. 결과는 항상 최신순으로 정렬돼서
    돌아온다(messages[0]이 가장 최근 메일) — "가장 최근 메일 하나"만 필요하면
    max_results=1 로 호출해도 된다."""

    log.info("gmail_search_emails 호출 run_id=%s query=%r", run_id, query)

    access_token = await _resolve_token(run_id)
    if access_token is None:
        log.info("run_id=%s: credential 없음(미연결 또는 run 조회 실패)", run_id)
        return {"connected": False, "messages": []}

    messages = await gmail_api.search_messages(access_token, query, max_results)
    log.info("run_id=%s: 검색 결과 %d건", run_id, len(messages))

    return {
        "connected": True,
        "messages": messages,
    }


@mcp.tool()
async def gmail_get_email(run_id: str, message_id: str) -> dict:
    """검색으로 얻은 message_id 로 본문 전체를 가져온다."""
    access_token = await _resolve_token(run_id)
    if access_token is None:
        return {"connected": False}
    message = await gmail_api.get_message(access_token, message_id)
    return {"connected": True, "message": message}


@mcp.tool()
async def gmail_send_email(run_id: str, to: str, subject: str, body: str) -> dict:
    """메일 발송. WRITE 작업이므로 Agent 쪽에서 app/tools/registry.py 의 승인 흐름을 태워야 한다
    (auto 모드 자동 통과 대상이 아니다 — 상대방에게 실제로 메일이 나간다)."""
    access_token = await _resolve_token(run_id)
    if access_token is None:
        return {"connected": False}
    result = await gmail_api.send_message(access_token, to, subject, body)
    return {"connected": True, "messageId": result.get("id")}


@mcp.tool()
async def gmail_connection_status(run_id: str) -> dict:
    """Agent가 '메일 보내줘' 요청을 받았는데 미연결일 때, 사용자에게 연결부터 안내하려고 쓴다."""
    access_token = await _resolve_token(run_id)
    return {"connected": access_token is not None}


@mcp.tool()
async def gmail_connect_url(run_id: str) -> dict:
    """★ 2026-08-13 추가 — gmail_connection_status 로 미연결을 확인했으면 이걸 불러서
    실제 Google 로그인 URL을 받아라. 이 URL 자체엔 토큰이나 비밀정보가 없다(서명된
    state 값만 담긴다) — 사용자가 클릭하면 Google 동의화면으로 이동해서 연동을
    끝낼 수 있다. app/tools/navigate.py 의 navigate(targetScreen=..., params={
    "authorizeUrl": 이 함수가 준 URL}) 로 넘겨서 사용자에게 클릭 버튼으로
    보여줘라 — URL을 텍스트로 그대로 답하지 마라(절대 규칙 2번과 같은 이유:
    말로만 하면 화면에 클릭 가능한 버튼이 안 생긴다).

    run_id→user_id 조회 자체가 실패하면(run 만료 등) {"error": "..."} 를 돌려준다
    — 이땐 URL 없이 "잠시 후 다시 시도해달라"고 안내하라."""
    try:
        user_id = await run_resolver.resolve_user_id(run_id)
    except run_resolver.RunResolutionError as exc:
        log.warning("run_id=%s → user_id 조회 실패(연동 URL 발급 불가): %s", run_id, exc)
        return {"error": "run_id_resolution_failed"}

    state = state_token.issue(user_id)
    url = google_oauth.build_authorize_url(state)
    log.info("connect-url 발급(채팅 경로) run_id=%s → user_id=%s", run_id, user_id)
    return {"authorizeUrl": url}
