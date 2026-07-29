"""
설정값 창고

민감한 값(API 키 등)과 환경마다 달라지는 값(백엔드 주소 등)을 코드에 박지 않고
.env 파일에서 읽어 한곳에 모은다. 앱 전체가 `from app.config import settings` 로 사용.

담당자 1. Pydantic Settings v2.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    llm_provider: str = "anthropic"           # anthropic | openai | google
    llm_model: str = "claude-sonnet-4"        # 실제 모델명은 팀 합의 후 확정
    llm_api_key: str = ""                     # ★ .env 에서 주입 (코드에 박지 말 것)
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 2

    # ── 백엔드 연동 (AI가 데이터 되물을 때 = /api/internal/v1/...) ──
    backend_base_url: str = "http://localhost:8080"

    # ── Engine B 안전장치 ──
    worker_max_tool_calls: int = 5            # Worker당 Tool 자율호출 상한
    replan_max_retries: int = 5               # HITL replan 재시도 상한


# 앱 전체가 공유하는 단일 인스턴스
settings = Settings()
