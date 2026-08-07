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
