# mcp_servers/gmail_mcp/tests/manual_gmail_flow.py
"""수동 E2E 테스트 — gmail-mcp 서버가 실제로 Google과 통신하는지 눈으로 확인용.

pytest 대상이 아니다 (자동화 불가 — 브라우저 로그인이 껴 있음). 터미널에서 직접 실행.

사용법
------
1) 이 서버를 먼저 띄운다 (다른 터미널):
     uvicorn mcp_servers.gmail_mcp.server:app --port 8100 --reload

2) .env 에 GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REDIRECT_URI /
   GMAIL_MCP_STATE_SECRET / GMAIL_MCP_TOKEN_ENCRYPTION_KEY / INTERNAL_API_KEY 채웠는지 확인.
   (GOOGLE_REDIRECT_URI 는 Google Cloud Console에 등록한 값과 바이트 단위로 같아야 함)

   ★ BE의 run_id→user_id API가 아직 없으므로 GMAIL_MCP_DEV_RUN_PASSTHROUGH=true 도
     .env 에 넣어둘 것 — 이 스크립트가 쓰는 TEST_RUN_ID 가 그대로 user_id 로 취급된다.
     BE API 붙으면 이 값을 지우고, TEST_RUN_ID 를 BE가 실제로 발급한 run_id 로 바꿔서
     같은 스크립트를 그대로 재사용하면 된다.

3) 이 스크립트 실행:
     python -m mcp_servers.gmail_mcp.tests.manual_gmail_flow

4) 스크립트가 출력하는 URL을 브라우저에 붙여넣고, "테스트 사용자"로 등록한 Gmail 계정으로
   로그인 → 동의화면에서 승인.

5) 스크립트가 자동으로 연결 완료를 감지하고, gmail_search_emails 툴을 호출해
   받은편지함 최신 메일 몇 건을 출력한다.
"""

from __future__ import annotations

import asyncio
import sys
import time

import httpx

BASE_URL = "http://localhost:8100"
TEST_RUN_ID = "manual-test-run"


async def main() -> None:
    from mcp_servers.gmail_mcp.config import get_settings

    settings = get_settings()
    if not settings.internal_api_key:
        print("INTERNAL_API_KEY 가 .env 에 없음. 먼저 채워주세요.")
        sys.exit(1)
    if not settings.dev_run_id_passthrough:
        print(
            "GMAIL_MCP_DEV_RUN_PASSTHROUGH=true 가 .env 에 없음. "
            "BE의 run_id→user_id API가 아직이라면 이 플래그를 켜야 이 스크립트가 동작합니다."
        )
        sys.exit(1)

    headers = {"X-Internal-Api-Key": settings.internal_api_key}

    # 1) connect-url 발급 (② 버튼 클릭을 대신함) — user_id가 아니라 run_id를 보낸다.
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        resp = await client.post(
            "/internal/gmail/connect-url", json={"run_id": TEST_RUN_ID}, headers=headers
        )
    resp.raise_for_status()
    authorize_url = resp.json()["authorize_url"]

    print("\n1) 아래 URL을 브라우저에 열고 테스트 사용자 Gmail로 로그인/승인하세요:\n")
    print(authorize_url)
    print("\n승인이 끝나면 이 창은 자동으로 감지합니다 (최대 5분 대기)...\n")

    # 2) ⑦ 저장 완료(연결됨)를 폴링으로 확인
    connected = False
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10.0) as client:
        for _ in range(60):
            status_resp = await client.get(f"/internal/gmail/status/{TEST_RUN_ID}", headers=headers)
            status_resp.raise_for_status()
            data = status_resp.json()
            if data.get("connected"):
                connected = True
                print(f"연결 완료: {data.get('googleEmail')}\n")
                break
            time.sleep(5)

    if not connected:
        print("5분 안에 연결이 확인되지 않았습니다. /oauth/gmail/callback 쪽 서버 로그를 확인하세요.")
        sys.exit(1)

    # 3) ⑧ MCP tool 직접 호출 — Agent를 거치지 않고 gmail-mcp의 /mcp 를 바로 두드린다
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(f"{BASE_URL}/mcp/") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            test_queries = [
                ("받은편지함", "in:inbox"),
                ("안 읽은 메일", "is:unread"),
                ("최근 메일", "after:2026/08/08"),
                # ("특정 사람", "from:someone@gmail.com"),
                # ("제목 검색", "subject:회의"),
                # ("특정 사람과 주고받은 메일", "from:someone@gmail.com OR to:someone@gmail.com"),
            ]

            for name, query in test_queries:
                print(f"\n===== {name} =====")
                print(f"query: {query}")

                result = await session.call_tool(
                    "gmail_search_emails",
                    {"run_id": TEST_RUN_ID, "query": query, "max_results": 5},
                )

                print("RESULT:", result)
                print("CONTENT:", result.content)

                for block in result.content:
                    print("BLOCK:", repr(block))
                    print("TEXT:", getattr(block, "text", None))


if __name__ == "__main__":
    asyncio.run(main())
