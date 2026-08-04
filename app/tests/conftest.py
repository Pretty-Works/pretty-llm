# app/tests/conftest.py
"""테스트 공통 설정.

Engine B 테스트는 LLM 없이 돌아야 한다. 그래서
- 기준일(AS_OF)을 고정하고
- 백엔드 미설정 상태(픽스처 모드)를 강제한다.
"""

import pytest

from app.config import get_settings
from app.engine_b.context_builder import build_context
from app.schemas.state import AnalysisPlan, AnalysisRequest, Entities, UIContext

# 데모 데이터가 이 날짜를 기준으로 설계돼 있다.
FIXED_TODAY = "2026-07-27"


@pytest.fixture(autouse=True)
def fixed_environment(monkeypatch):
    """기준일 고정 + 픽스처 모드 강제."""
    monkeypatch.setenv("AS_OF", FIXED_TODAY)
    monkeypatch.delenv("BACKEND_BASE_URL", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def project_plan() -> AnalysisPlan:
    return AnalysisPlan(
        mode="analysis",
        domains=["project", "hcm"],
        focus=["risk"],
        objective="그룹웨어 리뉴얼 프로젝트의 위험을 점검한다",
        entities=Entities(project_ids=[1001]),
    )


@pytest.fixture
def request_p001() -> AnalysisRequest:
    return AnalysisRequest(
        query="그룹웨어 리뉴얼 지금 위험한 거 있어?",
        user_id=1,
        ui_context=UIContext(screen="project_detail", project_id=1001),
    )


@pytest.fixture
def context_p001(project_plan, request_p001):
    """p001 기준 실제 컨텍스트 (픽스처 데이터로 조립)."""
    return build_context(project_plan, request_p001)
