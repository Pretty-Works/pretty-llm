"""
설정값 창고

민감한 값(API 키 등)과 환경마다 달라지는 값(백엔드 주소 등)을 코드에 박지 않고
.env 파일에서 읽어 한곳에 모은다. 앱 전체가 `from app.config import settings` 로 사용.

담당자 1. Pydantic Settings v2.
"""

from __future__ import annotations

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# .env 를 os.environ 에 로드한다. (교재의 load_dotenv 방식)
# langchain init_chat_model 이 OPENAI_API_KEY 를 환경변수에서 자동으로 읽으므로,
# API 키는 여기서 따로 관리하지 않고 환경변수로만 둔다.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # .env에 정의 안 한 키가 있어도 무시
    )

    # ── 앱 기본 ──
    app_name: str = "pretty-llm agent server"
    debug: bool = False

    # ── LLM ──
    #   API 키는 .env 의 OPENAI_API_KEY 로 두면 langchain 이 자동 사용.
    llm_provider: str = "openai"              # openai | anthropic | google
    llm_model: str = "gpt-4o-mini"            # 실제 모델명은 팀 합의 후 확정
    llm_api_key: str | None = None            # 엔진 B 쪽이 명시 참조. None 이면 환경변수 폴백
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 2

    # ── 백엔드 연동 (내부 도구 API = /api/internal/agent/**) ──
    #   규격: ⭐ 내부 도구 API 공동 규격 (FastAPI → Spring)
    backend_base_url: str = "http://localhost:3001"   # Spring BE
    internal_api_key: str = ""                        # X-Internal-Api-Key. 아직 미수령
    mock_backend: bool = True                         # true면 HTTP 없이 고정값 반환
    backend_connect_timeout_s: float = 3.0
    backend_read_timeout_s: float = 20.0
    tool_call_limit: int = 20                         # 실행당 도구 호출 상한 (규격 §6)

    # ── HITL checkpointer (승인 대기 상태 보관) ──
    #   InMemorySaver 를 쓰면 서버 재시작 시 승인 대기 건이 사라져
    #   2차 요청(resume)이 thread_id 를 못 찾는다. Docker 재배포마다 터지므로 파일로 둔다.
    checkpoint_db: str = "data/checkpoints.sqlite"

    # ── Engine B 안전장치 ──
    worker_max_tool_calls: int = 5            # Worker당 Tool 자율호출 상한
    replan_max_retries: int = 5               # HITL replan 재시도 상한


# 앱 전체가 공유하는 단일 인스턴스
settings = Settings()
