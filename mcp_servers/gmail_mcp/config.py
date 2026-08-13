# mcp_servers/gmail_mcp/config.py
"""Gmail MCP 서버 전용 설정.

app/config.py 와 분리한다 — 이 서버는 별도 프로세스(별도 배포 단위)로 뜨고,
Google client secret · 토큰 암호화 키처럼 Agent 프로세스가 절대 몰라도 되는
비밀을 담기 때문이다. .env 는 공유하되 키 프리픽스로 구분한다.
"""

from __future__ import annotations

from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


class GmailMcpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ─── Google OAuth 앱 등록 정보 (Google Cloud Console) ───────────
    google_client_id: str = ""
    google_client_secret: str = ""
    # Google 콘솔에 등록한 값과 바이트 단위로 같아야 한다.
    google_redirect_uri: str = "http://localhost:8100/oauth/gmail/callback"

    # 요청할 스코프. 읽기 전용으로 시작하고 발송이 필요해지면 넓힌다.
    gmail_scopes: str = (
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.send"
    )

    # ─── 이 서버가 붙는 곳 ────────────────────────────────────────
    # 8080: 이 MCP 서버 자체 포트. 8100 이 아니라 8080 이면 바꿔 쓴다.
    mcp_server_host: str = "0.0.0.0"
    mcp_server_port: int = 8100

    # ─── MCP 엔드포인트(/mcp) Host 헤더 검증 (DNS rebinding 방지) ────
    # ★ 2026-08-13 발견된 버그 — mcp_tools.py 의 FastMCP(...) 가 host= 를 안 넘기면
    #   SDK 기본값 "127.0.0.1"로 판단해서 자동으로
    #   allowed_hosts=["127.0.0.1:*","localhost:*","[::1]:*"] 만 허용해버린다
    #   (mcp/server/fastmcp/server.py __init__ 참고. 이 host 값은 uvicorn 바인딩
    #   주소인 mcp_server_host 와는 별개다 — 순수히 이 DNS-rebinding 방지용
    #   Host 헤더 검증에만 쓰인다). Docker Compose 내부망에서 Agent가 이 서버를
    #   "gmail-mcp:8100" 이라는 Host 헤더로 부르니 위 목록에 없어 매번
    #   421 Invalid Host header 로 막혔다. 실제 배포 토폴로지의 호스트명을
    #   여기서 쉼표로 열거한다 — 배포 주소가 바뀌면 .env 로 덮어쓰면 된다.
    mcp_allowed_hosts: str = Field(
        default="localhost:8100,127.0.0.1:8100,gmail-mcp:8100",
        validation_alias="GMAIL_MCP_ALLOWED_HOSTS",
    )

    # 연결 완료 후 사용자를 돌려보낼 Company Copilot 프론트 URL
    frontend_success_redirect: str = "http://localhost:3000/settings/integrations?gmail=connected"
    frontend_failure_redirect: str = "http://localhost:3000/settings/integrations?gmail=failed"

    # ─── state 파라미터 서명 (CSRF 방지 + "누가 연결 요청했는지" 증명) ─
    # 메인 백엔드가 짧게 살아있는 서명된 state를 만들어 "Gmail 연결하기" 링크에 심어준다.
    # 이 MCP 서버는 같은 secret으로 서명을 검증해 Company Copilot user_id를 신뢰한다.
    # (메인 백엔드 세션을 이 서버가 직접 조회할 필요가 없다 — 결합도를 낮추는 지점.)
    oauth_state_secret: str = Field(default="", validation_alias="GMAIL_MCP_STATE_SECRET")
    oauth_state_ttl_s: int = 600  # state 유효시간(초). 이 안에 로그인까지 끝내야 함

    # ─── 메인 백엔드 ↔ 이 서버 인증 ────────────────────────────────
    # app/config.py 의 internal_api_key 와 같은 값을 넣는다 — 메인 백엔드가
    # 이미 갖고 있는 헤더를 그대로 재사용해 관리 포인트를 늘리지 않는다.
    internal_api_key: str = ""

    # ─── 토큰 저장소 ──────────────────────────────────────────────
    # refresh_token 은 DB에 평문으로 두지 않는다. Fernet 대칭키로 암호화해 저장한다.
    # 키 생성: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = Field(default="", validation_alias="GMAIL_MCP_TOKEN_ENCRYPTION_KEY")
    credential_db_path: str = Field(
        default="data/gmail_credentials.sqlite",
        validation_alias="GMAIL_MCP_CREDENTIAL_DB_PATH",
    )

    # ─── run_id → user_id 조회 (Spring BE) ──────────────────────────
    # Gmail MCP는 user_id를 Agent/LLM에게서 직접 받지 않는다. 대신 run_id만 받아서
    # 이 BE 엔드포인트에 물어 user_id로 역산한다(run_resolver.py). Agent가 이미 쓰고
    # 있는 것과 같은 Spring BE라서, 값도 app/config.py의 backend_base_url과 동일하게
    # BACKEND_BASE_URL 환경변수를 그대로 재사용한다 — 관리 포인트를 늘리지 않으려는 의도.
    backend_base_url: str = Field(default="http://localhost:3001", validation_alias="BACKEND_BASE_URL")
    # ★ 2026-08-12 BE 확정 — GET /api/internal/agent/runs/{runId}/user.
    #   ({run_id}는 str.format으로 치환하는 자리라 스네이크케이스 그대로 둔다 — BE
    #   문서의 {runId} 표기와 이름만 다를 뿐 같은 자리다.) .env 로 얼마든지 덮어쓸 수
    #   있지만, 기본값 자체도 실제 확정된 경로로 맞춰 둔다.
    run_lookup_path_template: str = Field(
        default="/api/internal/agent/runs/{run_id}/user",
        validation_alias="GMAIL_MCP_RUN_LOOKUP_PATH_TEMPLATE",
    )
    run_lookup_timeout_s: float = 5.0

    # BE의 run_id→user_id API가 아직 없는 동안 로컬 개발/수동 테스트가 막히지 않도록 두는
    # 임시 우회로. true면 run_id를 그대로 user_id로 취급한다(run_resolver.py 참고).
    # ⚠️ BE API 붙으면 제일 먼저 꺼야 하는 플래그 — 운영 .env 에는 절대 true로 두지 않는다.
    dev_run_id_passthrough: bool = Field(
        default=False, validation_alias="GMAIL_MCP_DEV_RUN_PASSTHROUGH"
    )

    def validate_required(self) -> None:
        """부팅 시 필수값 누락을 바로 터뜨린다 — 운영 중 첫 OAuth 콜백에서 터지면 늦다."""
        missing = [
            name
            for name, val in (
                ("GOOGLE_CLIENT_ID", self.google_client_id),
                ("GOOGLE_CLIENT_SECRET", self.google_client_secret),
                ("GMAIL_MCP_STATE_SECRET", self.oauth_state_secret),
                ("GMAIL_MCP_TOKEN_ENCRYPTION_KEY", self.token_encryption_key),
                ("INTERNAL_API_KEY", self.internal_api_key),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                f"gmail_mcp: 필수 환경변수 누락 — {', '.join(missing)}. "
                ".env.example 의 [Gmail MCP] 섹션 참고."
            )


@lru_cache(maxsize=1)
def get_settings() -> GmailMcpSettings:
    return GmailMcpSettings()
