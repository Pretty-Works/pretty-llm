# app/tests/test_router.py
"""Analysis Router 테스트 (LLM 호출 없음).

라우터는 Engine B 의 입구라 여기서 틀리면 뒤 워커가 통째로 헛돈다.
LLM 응답 자체는 검증할 수 없으니, **LLM 출력을 코드가 어떻게 바로잡는지**를 검증한다.
"""

import pytest

from app.engine_b import analysis_router
from app.engine_b.analysis_router import _fallback_plan, _normalize, route
from app.schemas.state import AnalysisPlan, AnalysisRequest, Entities, UIContext


def _request(query: str, **ui) -> AnalysisRequest:
    return AnalysisRequest(query=query, user_id=1, ui_context=UIContext(**ui))


# ─── _normalize : LLM 이 흔히 어긋나게 내는 값을 코드가 잡아준다 ────────

def test_알수없는_도메인과_focus는_버린다():
    # 스키마 검증을 우회해 들어온 값(구조화 출력이 뚫렸거나 폴백이 만든 값)을 흉내낸다.
    plan = AnalysisPlan.model_construct(
        mode="analysis",
        domains=["project", "finance", "hcm"],  # finance 는 없는 도메인
        focus=["risk", "vibes"],  # vibes 는 없는 축
        objective="테스트",
        entities=AnalysisPlan().entities,
        constraints=[],
        reasoning="",
        confidence=0.5,
    )
    result = _normalize(plan, _request("아무거나"))

    assert result.domains == ["project", "hcm"]
    assert result.focus == ["risk"]


def test_도메인이_비면_project로_떨어진다():
    result = _normalize(AnalysisPlan(domains=[]), _request("음"))
    assert result.domains == ["project"]


def test_중복_도메인은_한_번만_남는다():
    result = _normalize(AnalysisPlan(domains=["project", "project", "hcm"]), _request("음"))
    assert result.domains == ["project", "hcm"]


def test_화면에_보고있는_프로젝트를_대상으로_채운다():
    plan = AnalysisPlan(domains=["project"])
    result = _normalize(plan, _request("이거 어때?", screen="project_detail", project_id=1001))

    assert result.entities.project_ids == [1001]


def test_이미_대상이_있으면_화면_컨텍스트로_덮어쓰지_않는다():
    plan = AnalysisPlan(domains=["project"], entities=Entities(project_ids=[1002]))
    result = _normalize(plan, _request("저거 어때?", project_id=1001))

    assert result.entities.project_ids == [1002]


def test_objective가_비면_질문을_그대로_쓴다():
    result = _normalize(AnalysisPlan(objective=""), _request("예산 얼마 남았어?"))
    assert result.objective == "예산 얼마 남았어?"


@pytest.mark.parametrize("raw,expected", [(1.7, 1.0), (-0.4, 0.0), (0.62, 0.62)])
def test_confidence는_0과_1_사이로_잘린다(raw, expected):
    result = _normalize(AnalysisPlan(confidence=raw), _request("음"))
    assert result.confidence == pytest.approx(expected)


# ─── 폴백 : LLM 을 못 쓰는 상황에서도 그래프가 끝까지 돈다 ────────

def test_재계획_표현이면_replan으로_본다():
    plan = _fallback_plan(_request("목표일을 2주 당겨야 하는데 조정안 좀 뽑아줘"))
    assert plan.mode == "replan"


def test_현황_질문은_analysis로_본다():
    plan = _fallback_plan(_request("지금 프로젝트 진행 상황 알려줘"))
    assert plan.mode == "analysis"


def test_사람_관련_질문이면_hcm이_들어간다():
    plan = _fallback_plan(_request("이 일에 누구를 배치하면 좋을까?"))
    assert "hcm" in plan.domains


def test_회의_질문이면_meeting이_들어간다():
    plan = _fallback_plan(_request("다음주 회의 시간대 잡아줘"))
    assert "meeting" in plan.domains


def test_폴백은_확신도를_낮게_준다():
    plan = _fallback_plan(_request("아무 질문"))
    assert plan.confidence <= 0.3


def test_LLM이_죽어도_라우팅은_계속된다(monkeypatch):
    """키가 없거나 API 가 죽어도 Engine B 는 멈추지 않아야 한다."""

    def boom(*args, **kwargs):
        raise RuntimeError("OPENAI_API_KEY 없음")

    monkeypatch.setattr(analysis_router.llm_client, "structured_call", boom)

    plan = route(_request("그룹웨어 리뉴얼 위험 알려줘", project_id=1001))

    assert plan.domains  # 비어 있지 않다
    assert plan.entities.project_ids == [1001]
    assert plan.confidence <= 0.3


def test_force_mode는_라우팅_결과를_덮어쓴다(monkeypatch):
    """담당자 3의 시나리오 재분석은 mode 를 다시 판단할 필요가 없다."""
    monkeypatch.setattr(
        analysis_router.llm_client,
        "structured_call",
        lambda *a, **k: AnalysisPlan(mode="analysis", domains=["project"]),
    )

    plan = route(_request("조정안 비교"), force_mode="replan")
    assert plan.mode == "replan"
