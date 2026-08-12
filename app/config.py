# app/config.py
"""전역 설정.

.env 를 읽어 앱 전체가 공유하는 설정값을 만든다.
Engine A / Engine B / Orchestrator 모두 여기서 값을 가져다 쓴다.

앱 계층은 `settings`, 엔진·워커는 `get_settings()` 를 쓴다. 같은 인스턴스다.
pydantic-settings v2 라 필드명 대문자가 곧 환경변수명이다.
"""

from datetime import date
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# langchain 이 OPENAI_API_KEY 를 환경변수에서 직접 읽는 경로가 있어 먼저 로드해 둔다.
load_dotenv()


class Settings(BaseSettings):
    """환경변수로 주입되는 런타임 설정."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env 에 정의 안 한 키가 있어도 무시
    )

    # ─── 앱 기본 ──────────────────────────────────────────────────
    app_name: str = "pretty-llm agent server"
    debug: bool = False

    # ─── LLM ──────────────────────────────────────────────────────
    llm_provider: str = "openai"   # openai | anthropic | google
    llm_model: str = "gpt-4o-mini"  # 단일 모델을 쓰는 코드용
    # Engine B 는 호출량이 많은 워커와 판단이 중요한 라우터/합성을 나눠 쓴다.
    llm_model_worker: str = "gpt-4o-mini"
    llm_model_reasoning: str = "gpt-4o"
    openai_model: str = "gpt-4o"   # api/meeting.py
    llm_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY"),
    )
    llm_temperature: float = 0.0
    llm_timeout: int = 60
    llm_timeout_s: float = 30.0    # dependencies.py 의 httpx 타임아웃
    llm_max_retries: int = 2

    # ─── 백엔드 연동 (내부 도구 API = /api/internal/agent/**) ─────
    backend_base_url: str = "http://localhost:3001"   # Spring BE
    internal_api_key: str = ""                        # 나갈 때 실어 보낸다. BE 가 요구
    # 들어오는 요청 검증용 — 나가는 키와 분리한다. 둘을 한 값으로 묶으면 BE 호출용
    # 키를 채우는 순간 수신 검증까지 켜져 Spring→FastAPI 요청이 전부 401 이 된다.
    # ★ 2026-08-12 — 지금은 의도적으로 비워둔 채 운영한다. Spring→FastAPI 인증
    #   헤더 없이 이미 배포됐고, 그 방향은 docker-compose 의 127.0.0.1 바인딩
    #   (네트워크 경계)로만 막기로 정했다 — app/common/auth.py 모듈 docstring 참고.
    #   나중에 배포 구조가 바뀌면(Spring 이 도커 내부망으로 접근하는 등) 이 값을
    #   채우고 BE 와 헤더 전송을 맞추면 바로 검증이 켜진다.
    inbound_api_key: str = ""
    mock_backend: bool = True                         # true면 HTTP 없이 고정값 반환
    backend_connect_timeout_s: float = 3.0
    backend_read_timeout_s: float = 20.0
    backend_timeout: int = 10
    backend_token: str | None = None
    tool_call_limit: int = 20      # 실행당 도구 호출 상한 (규격 §6)

    # ─── HITL checkpointer (승인 대기 상태 보관) ──────────────────
    #   InMemorySaver 를 쓰면 서버 재시작 시 승인 대기 건이 사라져
    #   2차 요청(resume)이 thread_id 를 못 찾는다. Docker 재배포마다 터지므로 파일로 둔다.
    checkpoint_db: str = "data/checkpoints.sqlite"

    # ─── Engine B 동작 파라미터 ───────────────────────────────────
    # 워커 1개가 근거 확보를 위해 자율적으로 툴을 호출할 수 있는 최대 횟수
    worker_max_tool_calls: int = 5
    # Validator 위반 시 워커를 다시 돌리는 최대 횟수
    # (2였으나 실측상 3번째 attempt도 위반을 다 못 고치는 경우가 잦고, replan 은
    #  시나리오 3개 × 워커 5개가 겹쳐 재시도 1회분만 줄여도 순간 토큰 사용량이
    #  크게 줄어든다 — TPM 레이트리밋 완화 목적)
    validator_max_retries: int = 1
    # HITL 에서 사용자가 조정안을 거절하고 재생성을 요청할 수 있는 최대 횟수
    replan_max_retries: int = 5
    # 이 값을 넘으면 과부하로 본다 (1.0 = 정원 100%)
    over_allocation_threshold: float = 1.2
    # 마감 임박 판정 기준(일)
    due_soon_days: int = 7
    # 워커 confidence 가 이 값 미만이면 Validator 가 warning 을 붙인다
    low_confidence_threshold: float = 0.45

    # ─── Gmail MCP 연동 ───────────────────────────────────────────
    #   토큰은 이 프로세스가 절대 안 갖는다. mcp_servers/gmail_mcp/ 가 별도로 들고 있고,
    #   Agent는 그 서버의 MCP 엔드포인트에 붙어 tool 목록만 받아 쓴다.
    gmail_mcp_server_url: str = "http://localhost:8100"   # 내부망 URL 권장 (외부 노출 X)

    # ─── 기타 ─────────────────────────────────────────────────────
    log_level: str = "INFO"
    # 데모/테스트 재현성을 위해 "오늘"을 고정하고 싶을 때 (YYYY-MM-DD)
    as_of_override: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AS_OF", "AS_OF_OVERRIDE"),
    )

    @property
    def uses_fixtures(self) -> bool:
        """툴이 백엔드 대신 픽스처를 쓸지."""
        return self.mock_backend or not self.backend_base_url

    def data_source_status(self) -> dict[str, object]:
        """지금 무엇을 데이터로 쓰고 있는지. 기동 로그·/health 가 이걸 그대로 노출한다.

        ★ 이 값이 안 보여서 배포가 픽스처로 도는 걸 아무도 몰랐다(2026-08-11 사고).
          키 값은 절대 싣지 않는다 — 채워졌는지 여부만 낸다.
        """
        return {
            "mode": "fixtures" if self.uses_fixtures else "backend",
            "mockBackend": self.mock_backend,
            "backendBaseUrl": self.backend_base_url or None,
            "internalApiKeySet": bool(self.internal_api_key),
            "inboundApiKeySet": bool(self.inbound_api_key),
        }

    def as_of(self) -> date:
        """분석 기준일. 미지정 시 시스템 오늘 날짜."""
        if self.as_of_override:
            return date.fromisoformat(self.as_of_override)
        return date.today()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


# main.py 용. 임포트 시점 스냅샷이라 엔진·워커·테스트는 get_settings() 를 쓴다.
settings = get_settings()
