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
    # BE 가 실제로 발급한 키가 로컬 .env 에 들어있어도 테스트는 항상 "키 미설정"
    # 상태(인증 통과)로 시작해야 한다 — 안 그러면 헤더를 안 보내는 기존 엔드포인트
    # 테스트들(test_ai_summary.py 등)이 개발자 로컬 환경에 따라 401로 깨진다.
    # ★ delenv 가 아니라 빈 문자열로 setenv 하는 이유: app/config.py의 Settings는
    #   env_file=".env" 를 물고 있어서, os.environ 에서 지워도(delenv) pydantic-
    #   settings가 .env 파일을 직접 다시 읽어(dotenv 소스) 거기 적힌 진짜 키값을
    #   그대로 채워 넣는다 — .env 파일 자체는 monkeypatch 가 못 건드리기 때문에
    #   delenv 로는 안 지워진다. 빈 문자열로 명시적 setenv 하면 환경변수 소스가
    #   dotenv 소스보다 우선순위가 높아서 확실히 이긴다.
    #   인증 자체를 검증하는 테스트(test_internal_api_auth.py)는 이 안에서 필요한
    #   만큼 직접 setenv 해서 덮어쓴다.
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    monkeypatch.setenv("INBOUND_API_KEY", "")   # 수신 검증도 같은 이유로 꺼둔다
    # ★ delenv("BACKEND_BASE_URL") 만으로는 픽스처 모드가 안 된다 — 위 주석과 같은
    #   이유로 pydantic-settings 가 .env 를 다시 읽어 실제 주소를 채운다. 게다가
    #   clients/backend.py 는 uses_fixtures 가 아니라 mock_backend 만 본다.
    monkeypatch.setenv("MOCK_BACKEND", "true")
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
def fixture_backed_hr(monkeypatch):
    """휴가·일정 내부도구 자리에 픽스처를 끼운다.

    hr_tool 은 이제 실API 만 부른다 (2026-08-11 재설계 — 픽스처 폴백 제거).
    테스트는 그 API 자리에 demo_data 를 끼워 '백엔드가 답한 상태'를 만든다.
    프로덕션 코드에 픽스처 분기를 남기지 않으면서 같은 시나리오를 재현하는 방법이다.
    """
    from app.tools import demo_data, hr_tool

    names = {u["id"]: u["name"] for u in demo_data.USERS}

    async def leaves(user_ids, date_from, date_to):
        return [
            leave
            for uid in user_ids
            for leave in demo_data.list_leaves(
                user_id=uid, date_from=date_from, date_to=date_to, status="APPROVED"
            )
        ]

    async def schedules(user_ids, date_from, date_to):
        targets = set(user_ids)
        return [
            {
                "id": s["id"],
                "title": s["title"],
                "start_at": f"{s['date']}T{s['start_time']}:00",
                "end_at": f"{s['date']}T{s['end_time']}:00",
                "all_day": False,
                "participant_names": [names.get(p, "") for p in s["participants"]],
            }
            for s in demo_data.list_schedules(date_from=date_from, date_to=date_to)
            if targets & set(s["participants"])
        ]

    monkeypatch.setattr(hr_tool, "fetch_leaves", leaves)
    monkeypatch.setattr(hr_tool, "fetch_schedules", schedules)


@pytest.fixture
async def context_p001(project_plan, request_p001, fixture_backed_hr):
    """p001 기준 실제 컨텍스트 (픽스처 데이터로 조립)."""
    return await build_context(project_plan, request_p001)


@pytest.fixture
def captured(monkeypatch):
    """test_tools.py 전용 — backend.write 스파이.

    test_tools.py는 원래 pytest용이 아니라 `python -m app.tests.test_tools`로
    돌리게 짜여 있었다 — 그 파일의 main()이 backend.write를 손으로 몽키패치해
    captured 딕셔너리를 채우고, 각 테스트 함수에 인자로 직접 넘겨줬다. pytest로
    개별 테스트(예: test_params_bytes_identical)만 돌리면 main()이 아예 안 돌아서
    그 스파이가 안 심긴다 — "fixture 'captured' not found" 로 죽는다. main()의
    스파이 로직을 그대로 fixture로 옮겨 pytest 경로에서도 동일하게 동작하게 한다."""
    from app.clients import backend as backend_mod

    result: dict = {}
    original = backend_mod.backend.write

    async def spy(method, path, run_id, approval_token, body):
        result.update(method=method, path=path, run_id=run_id,
                      approval_token=approval_token, body=body)
        return await original(method, path, run_id=run_id,
                              approval_token=approval_token, body=body)

    monkeypatch.setattr(backend_mod.backend, "write", spy)
    return result
