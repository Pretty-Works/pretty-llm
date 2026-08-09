# app/tests/test_vacation_worker.py
"""엔진B의 vacation 도메인 워커 — "나 내일 연차 쓸건데 프로젝트에 지장 없어?",
"다음달에 2일 휴가 쓰고 싶은데 지장없게 날짜 추천해줘" 같은, 실행이 아니라
분석 결과 자체가 최종 답인 휴가 질문을 담당한다.

★ 배경
  app/workers/registry.py 에는 원래 "vacation": [...] 자리만 주석으로 비어
  있었다(담당자1 몫으로 표시됨) — registry.specs_for_domains(["vacation"])가
  빈 배열을 돌려줬다는 뜻이라, analysis_router가 아무리 vacation을 잘 골라도
  실제로 도는 워커가 하나도 없었다. app/workers/vacation/vacation_worker.py에
  실제 워커를 채우고 registry에 등록했다.

  단순 실행 요청("연차 신청해줘")은 여전히 engine_a의 leave_agent
  (app/engine_a/leave_agent.py, 이미 존재)가 처리한다 — classify.py의 기존
  규칙(실행 동사가 있으면 engine_a)이 그대로 적용되므로 손댈 필요가 없었다.
"""

from __future__ import annotations

from datetime import date

from app.engine_b.context_builder import build_context
from app.prompts import analysis_router as router_prompts
from app.prompts import vacation as vacation_prompt
from app.schemas.state import AnalysisPlan, Entities
from app.tools.registry import is_write
from app.workers import registry
from app.workers.vacation.vacation_worker import SPEC, VacationImpactResult


# ─── 도메인 레지스트리 등록 ─────────────────────────────────────────

def test_vacation_registered_in_domain_registry():
    specs = registry.specs_for_domains(["vacation"])

    assert specs == [SPEC]
    assert specs[0].node_name == "vacation.impact"
    assert registry.unsupported_domains(["project", "vacation"]) == []


def test_vacation_findable_by_dimension():
    """Validator 가 위반 축만 재실행시킬 때 쓰는 조회 경로."""
    spec = registry.spec_by_dimension("impact")

    assert spec is not None
    assert spec.domain == "vacation"


# ─── 도구 — 쓰기 도구가 하나도 없어야 한다 ───────────────────────────

def test_vacation_worker_has_no_write_tools():
    """이 워커도 run_tool_loop()(승인 게이트 없음)를 거친다 — leave_create/
    leave_update 같은 쓰기 도구가 섞여 있으면 사람 승인 없이 실행돼 버린다.
    아예 import 를 안 했는지(가장 확실한 차단)와, 혹시 몰라 registry.is_write()
    기준으로도 한 번 더 확인한다."""
    assert SPEC.tools, "도구가 비어 있음 — 워커가 스스로 근거를 못 찾는다"
    for t in SPEC.tools:
        assert not is_write(t.name), f"{t.name} 은 쓰기 도구 — vacation 워커에 있으면 안 됨"


def test_vacation_worker_has_no_async_tools():
    """gmail 같은 부가 도구는 이 워커의 범위가 아니다(priority/risk와 다름) —
    의도치 않게 붙었는지 회귀로 고정."""
    assert SPEC.async_tools is None


# ─── 결과 스키마 ────────────────────────────────────────────────────

def test_vacation_result_defaults_to_clear_with_no_data():
    result = VacationImpactResult()

    assert result.verdict == "clear"
    assert result.conflicts == []
    assert result.recommended_windows == []


def test_vacation_result_holds_conflicts_and_recommendations():
    result = VacationImpactResult(
        requester_id=2,
        requested_start="2026-08-10",
        requested_end="2026-08-10",
        verdict="blocking",
        conflicts=[{
            "date": "2026-08-10",
            "kind": "deadline",
            "subject": "todo:101",
            "detail": "t101 마감일과 정확히 겹침",
            "severity": "blocking",
        }],
        recommended_windows=[{
            "start_date": "2026-08-12",
            "end_date": "2026-08-12",
            "reason": "그 주 마감이 없는 날",
            "residual_risk": "",
        }],
    )

    assert result.verdict == "blocking"
    assert result.conflicts[0].severity == "blocking"
    assert result.recommended_windows[0].start_date == "2026-08-12"


# ─── 프롬프트 ───────────────────────────────────────────────────────

def test_vacation_prompt_covers_key_judgment_points():
    assert "team_overlap" in vacation_prompt.METHOD
    assert "deadline" in vacation_prompt.METHOD
    assert "recommended_windows" in vacation_prompt.METHOD
    assert "verdict" in vacation_prompt.METHOD


def test_vacation_prompt_defers_execution_to_engine_a():
    """이 워커가 신청/승인을 대신하는 것처럼 답하면 안 된다는 안내가 있는지."""
    assert "신청" in vacation_prompt.ROLE or "신청" in vacation_prompt.METHOD


# ─── 라우터 few-shot ────────────────────────────────────────────────

def test_router_has_vacation_domain_few_shots():
    """analysis_router가 "누구누구 휴가가 프로젝트에 지장있나" 류 질문을
    이제 vacation 하나로 라우팅하도록 예시가 갱신됐는지 — 예시가 옛날처럼
    ["project","hcm"]를 계속 가리키면 워커를 만들어도 실제로는 안 골라진다."""
    vacation_only_shots = [
        shot for shot in router_prompts.FEW_SHOTS
        if shot["answer"]["domains"] == ["vacation"]
    ]
    assert len(vacation_only_shots) >= 2, "vacation 단독 라우팅 예시가 부족함"


def test_build_few_shot_text_still_renders():
    """few-shot 텍스트 직렬화가 새 예시들과 함께 예외 없이 도는지."""
    text = router_prompts.build_few_shot_text()
    assert "vacation" in text


# ─── Context Builder — vacation 도메인만으로도 인력 데이터가 채워지는지 ──

async def test_vacation_도메인이면_인력데이터를_긁는다(request_p001):
    """hcm 없이 vacation 만 골라도 context_builder._load_people()이 돌아야
    한다 — 이 워커가 팀원 휴가 겹침(team_overlap)을 보려면 leaves/workloads가
    필요하다."""
    plan = AnalysisPlan(domains=["vacation"], entities=Entities(project_ids=[1001]))
    context = await build_context(plan, request_p001)

    assert context.candidates  # 프로젝트 참여자가 후보로 잡혔어야 한다
    assert context.workloads   # 부하 지표가 계산됐어야 한다
