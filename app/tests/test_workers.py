# app/tests/test_workers.py
"""Worker Layer / Context Builder / Validator 테스트 (LLM 호출 없음).

워커 안의 LLM 판단은 여기서 검증할 수 없다. 대신 그 판단을 **감싸는 것들**을 검증한다.
- Context Builder 가 사실을 제대로 모으는가
- Validator 가 잘못된 워커 출력을 실제로 잡아내는가
- 재생성 루프가 올바른 축만 다시 돌리도록 신호를 만드는가
"""

from datetime import date

from app.engine_b.context_builder import build_context, render_context
from app.engine_b.validator import (
    SKILL_FIT_CONFIDENCE_CAP,
    validate,
    validate_synthesis,
)
from app.schemas.state import (
    AnalysisContext,
    AnalysisPlan,
    Entities,
    ProposedChange,
    SynthesisResult,
    WorkerOutput,
    merge_worker_outputs,
)
from app.tools import demo_data
from app.workers import registry

AS_OF = date(2026, 7, 27)


def _output(dimension: str, result: dict, *, domain="project", confidence=0.8) -> WorkerOutput:
    from app.schemas.state import Evidence

    return WorkerOutput(
        dimension=dimension,
        domain=domain,
        result=result,
        reasoning="테스트용",
        confidence=confidence,
        evidence=[Evidence(source="context", ref="project:1001", detail="테스트")],
    )


def _codes(report) -> set[str]:
    return {v.code for v in report.violations}


# ─── 픽스처 데이터 자체의 규칙 ────────────────────────────────────

def test_예산은_결재중_금액까지_포함해_계산된다():
    """committed 를 빼먹으면 실제보다 여유 있어 보인다."""
    budget = demo_data.get_budget(1002)

    assert budget["spent"] == 31_200_000
    assert budget["committed"] == 6_500_000
    assert budget["total"] - budget["spent"] - budget["committed"] == 2_300_000


def test_잔여연차는_승인된_휴가만_차감한다():
    """ERD 원칙: 카운터를 따로 두지 않고 승인 문서 기준으로 계산한다."""
    # u003 은 8/24~8/28 휴가가 PENDING 이라 아직 차감되면 안 된다
    assert demo_data.leave_balance(3, 2026)["used"] == 0
    # u002 는 8/3~8/7 승인분 5일
    assert demo_data.leave_balance(2, 2026)["used"] == 5


def test_미승인_휴가는_가용성_조회에_잡히지_않는다():
    leaves = demo_data.list_leaves(3, date(2026, 8, 1), date(2026, 8, 31))
    assert leaves == []


# ─── Context Builder ──────────────────────────────────────────────

def test_컨텍스트에_프로젝트와_예산이_담긴다(context_p001):
    project = context_p001.project(1001)

    assert project is not None
    assert project.name == "그룹웨어 리뉴얼"
    assert len(project.members) == 5
    assert context_p001.budget(1001).remaining == 21_500_000


def test_진행률은_취소건을_분모에서_뺀다(context_p001):
    project = context_p001.project(1001)
    counted = [t for t in project.todos if t.status != "CANCELED"]
    done = [t for t in counted if t.status == "DONE"]

    assert project.progress == round(len(done) / len(counted), 3)


def test_분석기간은_목표일까지_잡힌다(context_p001):
    assert context_p001.window_from == AS_OF
    assert context_p001.window_to == date(2026, 9, 30)


def test_hcm_도메인이면_가용성지표를_코드로_계산해_넣는다(context_p001):
    """LLM 이 세지 않게 미리 세어둔다.

    ★ 집계 범위는 **컨텍스트에 실린 프로젝트 할 일**뿐이다 (2026-08-11 재설계).
      프로젝트 밖 할 일은 요청자가 화면에서 볼 수 없는 정보라 조회하지 않는다.
    """
    by_user = {w["user_id"]: w for w in context_p001.workloads}

    assert set(by_user) == {1, 2, 3, 5, 7}

    minju = by_user[2]
    assert minju["open_todo_count"] == 3  # t101, t106, t107 — t203(p002)은 범위 밖
    assert minju["overdue_count"] == 1  # t101 (마감 7/20)
    assert minju["approved_leave_days"] == 5  # 8/3~8/7
    assert minju["available_days"] == minju["working_days"] - 5


async def test_hcm이_없으면_인력데이터를_긁지_않는다(request_p001):
    """불필요한 조회를 막는다."""
    plan = AnalysisPlan(domains=["project"], entities=Entities(project_ids=[1001]))
    context = await build_context(plan, request_p001)

    assert context.workloads == []
    assert context.candidates == []
    assert context.projects  # 프로젝트는 그대로 있다


async def test_후보군은_프로젝트_참여자를_넘지_않는다(project_plan, request_p001, fixture_backed_hr):
    """★ 회귀 테스트 — 배포에서 존재하지 않는 직원이 분석에 섞인 사고의 방어선.

    예전에는 후보가 비거나 skill_fit 이 focus 에 있으면 전사 명부를 통째로 긁었다
    (`list_department_members` 무필터 호출). 그 경로로 프로젝트와 무관한 사람이
    후보군에 들어왔다. 이제 후보군은 프로젝트 참여자 + 질문에 이름이 나온 사람뿐이다.
    """
    project_plan.focus = ["skill_fit"]          # 예전에 전사 조회를 촉발하던 조건
    context = await build_context(project_plan, request_p001)

    member_ids = {m.user_id for p in context.projects for m in p.members}
    assert {c.user_id for c in context.candidates} <= member_ids
    assert len(context.candidates) == 5         # p001 참여자 그대로. 8명 전원이 아니다


async def test_참여자_밖_id는_조회하지_않고_기록만_남긴다(project_plan, request_p001, fixture_backed_hr):
    """id 로 남의 프로필을 여는 경로 자체를 없앴다."""
    project_plan.entities.user_ids = [999]
    context = await build_context(project_plan, request_p001)

    assert all(c.user_id != 999 for c in context.candidates)
    assert any("999" in item for item in context.missing)


async def test_me_도메인은_본인_데이터만_싣는다(request_p001, monkeypatch):
    """★ 배포 사고가 난 질문("뭐부터 할까")이 원래 갔어야 할 경로.

    본인 스코프 내부도구만 쓰므로 후보군·프로젝트 참여자가 섞이지 않는다.
    """
    from app.engine_b.context_builder import render_context
    from app.tools import hr_tool

    async def my_tasks(week_offset: int = 0):
        return {
            "week_start": "2026-07-27", "week_end": "2026-08-02",
            "tasks": [{"id": 58, "title": "API 명세 정리", "due_date": "2026-07-24",
                       "status": "TODO", "project_id": 1001}],
        }

    monkeypatch.setattr(hr_tool, "fetch_my_tasks", my_tasks)
    monkeypatch.setattr(hr_tool, "fetch_my_leave_balance",
                        lambda year=None: _async({"granted": 15, "used": 3, "remaining": 12}))
    monkeypatch.setattr(hr_tool, "fetch_my_schedules", lambda f, t: _async([]))

    plan = AnalysisPlan(domains=["me"], focus=["my_week"])
    context = await build_context(plan, request_p001)

    assert context.my_week is not None
    assert context.my_week.leave_remaining_days == 12
    assert [t.id for t in context.my_week.tasks] == [58]
    assert context.candidates == []          # 남의 데이터가 섞일 경로가 없다

    text = render_context(context, ("my_week",))
    assert "내 이번 주" in text and "D+3 지연" in text


def _async(value):
    """monkeypatch 용 — 코루틴으로 감싼다."""
    async def _wrapped(*args, **kwargs):
        return value
    return _wrapped()


def test_데이터게이트가_근거없는_축을_건너뛴다():
    """근거가 없으면 워커를 돌리지 않는다 — 예전에는 '도구로 직접 찾아라'고 넘겼다."""
    from app.engine_b.context_builder import apply_data_gate, skipped_dimensions

    context = AnalysisContext(as_of=AS_OF)      # 프로젝트·예산·후보 전부 없음
    apply_data_gate(context)

    assert skipped_dimensions(context) == {
        "priority", "risk", "cost", "skill_fit", "workload", "my_week"
    }


async def test_대상이_없으면_참여중인_프로젝트로_떨어진다(request_p001):
    request_p001.ui_context.project_id = None
    plan = AnalysisPlan(domains=["project"])

    context = await build_context(plan, request_p001)

    # u001 은 p001, p003 참여 (p000 은 COMPLETED 라 제외)
    assert {p.id for p in context.projects} == {1001, 1003}


def test_렌더링에_지연_표시가_들어간다(context_p001):
    text = render_context(context_p001)

    assert "101" in text
    assert "지연" in text  # t101 은 마감 7/20 으로 이미 지났다
    assert "결재중" in text  # 예산 섹션


def test_섹션을_지정하면_그것만_렌더링된다(context_p001):
    text = render_context(context_p001, ("project", "budget"))

    assert "## 예산" in text or "### 예산" in text
    assert "인력 부하 지표" not in text


# ─── Validator - priority ─────────────────────────────────────────

def test_없는_할일을_순위에_넣으면_잡는다(context_p001):
    output = _output(
        "priority",
        {"ranked": [{"task_id": 999, "title": "유령 작업", "tier": "P0", "is_overdue": False}]},
    )
    report = validate([output], context_p001)

    assert "UNKNOWN_TASK" in _codes(report)
    assert not report.ok


def test_완료된_할일을_순위에_넣으면_잡는다(context_p001):
    output = _output(
        "priority",
        {"ranked": [{"task_id": 105, "title": "디자인 토큰", "tier": "P1", "is_overdue": False}]},
    )
    report = validate([output], context_p001)

    assert "CLOSED_TASK_RANKED" in _codes(report)


def test_지연여부를_틀리게_적으면_잡는다(context_p001):
    """t101 은 마감 2026-07-20 으로 이미 지났다."""
    output = _output(
        "priority",
        {"ranked": [{"task_id": 101, "title": "권한 정책", "tier": "P0", "is_overdue": False}]},
    )
    report = validate([output], context_p001)

    assert "OVERDUE_FLAG_MISMATCH" in _codes(report)
    hint = next(v for v in report.errors if v.code == "OVERDUE_FLAG_MISMATCH").fix_hint
    assert "True" in hint  # 무엇으로 고쳐야 하는지 알려준다


def test_마감일을_임의로_바꾸면_잡는다(context_p001):
    output = _output(
        "priority",
        {
            "ranked": [
                {
                    "task_id": 101,
                    "title": "권한 정책",
                    "tier": "P0",
                    "is_overdue": True,
                    "due_date": "2026-09-01",
                }
            ]
        },
    )
    report = validate([output], context_p001)

    assert "DATE_MISMATCH" in _codes(report)


# ─── Validator - risk ─────────────────────────────────────────────

def test_한_칸에_여러_대상을_적어도_오탐하지_않는다(context_p001):
    """★ 회귀 — 앞 5글자만 잘라 비교하던 탓에 실제 존재하는 할 일을 '없다'고 잡았다.

    한 위험이 여러 할 일에 걸리는 건 정상이라 모델은 이렇게 몰아서 적는다.
    """
    output = _output(
        "risk",
        {
            "overall_risk_score": 60,
            "risks": [
                {
                    "category": "schedule",
                    "title": "일정 위험",
                    "subject": "todo:101, todo:102, todo:106",   # 셋 다 p001 에 실재한다
                    "likelihood": 80,
                    "impact": 50,
                    "risk_score": 40,
                    "mitigation": "담당 재배분",
                }
            ],
        },
    )
    report = validate([output], context_p001)

    assert "UNKNOWN_SUBJECT" not in _codes(report)


def test_여러_대상_중_없는_것만_골라_잡는다(context_p001):
    output = _output(
        "risk",
        {
            "overall_risk_score": 60,
            "risks": [
                {
                    "category": "schedule",
                    "title": "일정 위험",
                    "subjects": ["todo:101", "todo:9999"],
                    "likelihood": 80,
                    "impact": 50,
                    "risk_score": 40,
                    "mitigation": "담당 재배분",
                }
            ],
        },
    )
    report = validate([output], context_p001)

    unknown = [v for v in report.violations if v.code == "UNKNOWN_SUBJECT"]
    assert len(unknown) == 1
    assert unknown[0].subject == "todo:9999"


def test_위험점수_계산이_틀리면_잡는다(context_p001):
    output = _output(
        "risk",
        {
            "overall_risk_score": 60,
            "risks": [
                {
                    "category": "schedule",
                    "title": "일정 지연",
                    "subject": "todo:101",
                    "likelihood": 80,
                    "impact": 50,
                    "risk_score": 90,  # 실제로는 40
                    "mitigation": "담당 재배분",
                }
            ],
        },
    )
    report = validate([output], context_p001)

    assert "RISK_SCORE_MISMATCH" in _codes(report)
    assert "40" in next(v for v in report.errors if v.code == "RISK_SCORE_MISMATCH").fix_hint


def test_위험점수가_맞으면_통과한다(context_p001):
    output = _output(
        "risk",
        {
            "overall_risk_score": 45,
            "risks": [
                {
                    "category": "schedule",
                    "title": "일정 지연",
                    "subject": "todo:101",
                    "likelihood": 80,
                    "impact": 50,
                    "risk_score": 40,
                    "mitigation": "담당 재배분",
                }
            ],
        },
    )
    report = validate([output], context_p001)

    assert report.ok


def test_없는_대상을_지목하면_잡는다(context_p001):
    output = _output(
        "risk",
        {
            "overall_risk_score": 30,
            "risks": [
                {
                    "category": "resource",
                    "title": "인력 이탈",
                    "subject": "user:999",
                    "likelihood": 30,
                    "impact": 40,
                    "risk_score": 12,
                    "mitigation": "백업 인력 확보",
                }
            ],
        },
    )
    report = validate([output], context_p001)

    assert "UNKNOWN_SUBJECT" in _codes(report)


# ─── Validator - cost ─────────────────────────────────────────────

def _cost_result(**overrides) -> dict:
    base = {
        "budget_total": 120_000_000,
        "spent": 86_500_000,
        "committed": 12_000_000,
        "remaining": 21_500_000,
        "usage_ratio": 0.821,
        "elapsed_ratio": 0.64,
        "projected_total": 118_000_000,
        "overrun_amount": 0,
        "on_track": True,
        "drivers": [],
        "levers": [],
    }
    return {**base, **overrides}


def test_예산_수치를_지어내면_잡는다(context_p001):
    output = _output("cost", _cost_result(spent=50_000_000, remaining=58_000_000))
    report = validate([output], context_p001)

    assert "BUDGET_FIGURE_MISMATCH" in _codes(report)


def test_잔액_계산이_틀리면_잡는다(context_p001):
    output = _output("cost", _cost_result(remaining=40_000_000))
    report = validate([output], context_p001)

    assert "BUDGET_ARITHMETIC" in _codes(report)


def test_초과가_예상되는데_정상궤도라고_하면_잡는다(context_p001):
    output = _output("cost", _cost_result(overrun_amount=8_000_000, on_track=True))
    report = validate([output], context_p001)

    assert "BUDGET_EXCEEDED" in _codes(report)


def test_대가없는_절감안은_잡는다(context_p001):
    output = _output(
        "cost",
        _cost_result(
            levers=[{"action": "외주 축소", "expected_saving": 5_000_000, "tradeoff": ""}]
        ),
    )
    report = validate([output], context_p001)

    assert "LEVER_WITHOUT_TRADEOFF" in _codes(report)


def test_정상적인_비용분석은_통과한다(context_p001):
    output = _output(
        "cost",
        _cost_result(
            levers=[
                {
                    "action": "9월 클라우드 선결제 보류",
                    "expected_saving": 12_000_000,
                    "tradeoff": "요금 할인 혜택을 놓친다",
                }
            ]
        ),
    )
    report = validate([output], context_p001)

    assert report.ok


# ─── Validator - skill_fit ────────────────────────────────────────

def _assignment(user_id: str, **overrides) -> dict:
    """skill_fit 결과 1건. 점수·순위 필드는 없다 (2026-08-11 재설계)."""
    base = {
        "target": "todo:103",
        "target_kind": "task",
        "work_type": "FE",
        "matches": [
            {
                "user_id": user_id,
                "name": "테스트",
                "role": "FE",
                "basis": "이 프로젝트 FE 역할. 결재선 화면 개편을 처리했다",
                "note": "",
            }
        ],
    }
    return {**base, **overrides}


def test_적합도_확신도_상한을_넘으면_잡는다(context_p001):
    """스킬 데이터가 없는 추론 축이라 구조적으로 확신할 수 없다."""
    output = _output(
        "skill_fit",
        {"assignments": [_assignment(3)]},
        domain="hcm",
        confidence=0.92,
    )
    report = validate([output], context_p001)

    assert "CONFIDENCE_CAP_EXCEEDED" in _codes(report)
    assert str(SKILL_FIT_CONFIDENCE_CAP) in next(
        v for v in report.errors if v.code == "CONFIDENCE_CAP_EXCEEDED"
    ).fix_hint


def test_부재를_밝히지_않으면_잡는다(context_p001):
    """u002 는 8/3~8/7 승인 휴가가 있다. 배제 사유가 아니라 밝혀야 할 사실이다."""
    output = _output(
        "skill_fit",
        {"assignments": [_assignment(2)]},
        domain="hcm",
        confidence=0.7,
    )
    report = validate([output], context_p001)

    assert "MEMBER_UNAVAILABLE" in _codes(report)
    # 순위가 없는 구조라 배제가 아니라 경고다
    assert all(v.severity == "warning" for v in report.violations if v.code == "MEMBER_UNAVAILABLE")


def test_부재를_note에_적으면_통과한다(context_p001):
    assignment = _assignment(2)
    assignment["matches"][0]["note"] = "2026-08-03~2026-08-07 승인 휴가로 부재"
    output = _output(
        "skill_fit", {"assignments": [assignment]}, domain="hcm", confidence=0.7
    )
    report = validate([output], context_p001)

    assert "MEMBER_UNAVAILABLE" not in _codes(report)


def test_근거가_비면_잡는다(context_p001):
    assignment = _assignment(3)
    assignment["matches"][0]["basis"] = ""
    output = _output(
        "skill_fit", {"assignments": [assignment]}, domain="hcm", confidence=0.7
    )
    report = validate([output], context_p001)

    assert "NO_BASIS" in _codes(report)


def test_후보군_밖의_사람을_제시하면_잡는다(context_p001):
    """후보군 표가 전부다. 전사 명부를 긁던 경로를 없앤 뒤의 마지막 방어선."""
    output = _output(
        "skill_fit",
        {"assignments": [_assignment(999)]},
        domain="hcm",
        confidence=0.7,
    )
    report = validate([output], context_p001)

    assert "UNKNOWN_USER" in _codes(report)


def test_정상적인_역할_매칭은_통과한다(context_p001):
    output = _output(
        "skill_fit",
        {"assignments": [_assignment(3)]},
        domain="hcm",
        confidence=0.7,
    )
    report = validate([output], context_p001)

    assert report.ok


# ─── Validator - workload ─────────────────────────────────────────

def _member_load(context, user_id: str, **overrides) -> dict:
    actual = next(w for w in context.workloads if w["user_id"] == user_id)
    base = {
        "user_id": user_id,
        "name": actual["name"],
        "status": "TIGHT",
        "open_todo_count": actual["open_todo_count"],
        "overdue_count": actual["overdue_count"],
        "due_in_window_count": actual["due_in_window_count"],
        "approved_leave_days": actual["approved_leave_days"],
        "available_days": actual["available_days"],
        "load_index": actual["load_index"],
    }
    return {**base, **overrides}


def test_부하지표를_바꿔_적으면_잡는다(context_p001):
    """코드가 센 값을 모델이 바꾸면 이후 판단이 전부 어긋난다."""
    output = _output(
        "workload",
        {"members": [_member_load(context_p001, 2, overdue_count=0)]},
        domain="hcm",
    )
    report = validate([output], context_p001)

    assert "METRIC_MISMATCH" in _codes(report)


def test_지표를_그대로_인용하면_통과한다(context_p001):
    output = _output(
        "workload",
        {"members": [_member_load(context_p001, 2)], "bottlenecks": [2]},
        domain="hcm",
    )
    report = validate([output], context_p001)

    assert report.ok


def test_휴가중인_사람에게_일을_넘기라고_하면_잡는다(context_p001):
    output = _output(
        "workload",
        {
            "members": [_member_load(context_p001, 3)],
            "rebalance_hints": [
                {
                    "task_id": 101,
                    "from_user_id": 3,
                    "to_user_candidates": [2],  # 8/3~8/7 휴가
                    "reason": "여유 있어 보임",
                }
            ],
        },
        domain="hcm",
    )
    report = validate([output], context_p001)

    assert "MEMBER_UNAVAILABLE" in _codes(report)


# ─── 재생성 루프 신호 ─────────────────────────────────────────────

def test_위반이_난_축만_재생성_대상이_된다(context_p001):
    bad_priority = _output(
        "priority", {"ranked": [{"task_id": 999, "tier": "P0", "is_overdue": False}]}
    )
    good_cost = _output("cost", _cost_result())

    report = validate([bad_priority, good_cost], context_p001)

    assert report.dimensions_to_retry() == ["priority"]


def test_재생성_지시에는_수정방법이_담긴다(context_p001):
    output = _output(
        "priority",
        {"ranked": [{"task_id": 101, "tier": "P0", "is_overdue": False}]},
    )
    report = validate([output], context_p001)
    feedback = report.feedback_by_dimension()

    assert "priority" in feedback
    assert any("OVERDUE_FLAG_MISMATCH" in line for line in feedback["priority"])


def test_경고는_재생성을_유발하지_않는다(context_p001):
    """근거 없음/확신 낮음은 알려주되 다시 돌릴 일은 아니다."""
    output = WorkerOutput(
        dimension="cost", domain="project", result=_cost_result(), confidence=0.2
    )
    report = validate([output], context_p001)

    assert "LOW_CONFIDENCE" in _codes(report)
    assert "NO_EVIDENCE" in _codes(report)
    assert report.ok  # error 는 없다
    assert report.dimensions_to_retry() == []


def test_워커가_죽어도_검증은_계속된다(context_p001):
    dead = WorkerOutput(dimension="risk", domain="project", error="타임아웃", confidence=0.0)
    report = validate([dead], context_p001)

    assert "WORKER_ERROR" in _codes(report)
    assert report.ok  # 죽은 워커 때문에 재생성 루프를 돌지는 않는다


def test_죽은_워커의_빈결과를_규칙위반으로_보지_않는다(context_p001):
    """빈 result 를 '예산 0원'으로 읽으면 살아날 수 없는 워커를 한도까지 재시도하게 된다."""
    dead = WorkerOutput(dimension="cost", domain="project", error="API 401", confidence=0.0)
    report = validate([dead], context_p001)

    assert "BUDGET_FIGURE_MISMATCH" not in _codes(report)
    assert report.dimensions_to_retry() == []


def test_다른_워커가_죽어도_살아있는_워커는_검증된다(context_p001):
    dead = WorkerOutput(dimension="risk", domain="project", error="타임아웃")
    bad = _output(
        "priority", {"ranked": [{"task_id": 999, "tier": "P0", "is_overdue": False}]}
    )
    report = validate([dead, bad], context_p001)

    assert report.dimensions_to_retry() == ["priority"]


# ─── 상태 리듀서 ──────────────────────────────────────────────────

def test_재생성_결과가_이전_결과를_대체한다():
    """단순 append 면 같은 축의 낡은 결과가 남아 통합 단계가 헷갈린다."""
    first = WorkerOutput(dimension="priority", domain="project", confidence=0.3, attempt=1)
    retried = WorkerOutput(dimension="priority", domain="project", confidence=0.9, attempt=2)

    merged = merge_worker_outputs([first], [retried])

    assert len(merged) == 1
    assert merged[0].attempt == 2


def test_조정안이_다르면_같은_축이라도_따로_남는다():
    """담당자 3의 시나리오 3개가 서로를 덮어쓰면 안 된다."""
    a = WorkerOutput(dimension="priority", domain="project", scenario_id="A")
    b = WorkerOutput(dimension="priority", domain="project", scenario_id="B")

    assert len(merge_worker_outputs([a], [b])) == 2


# ─── 레지스트리 / 그래프 ──────────────────────────────────────────

def test_도메인을_고르면_그_세트가_전부_실행대상이_된다():
    """focus 는 강조점일 뿐 실행 여부와 무관하다 - 설계 원칙."""
    specs = registry.specs_for_domains(["project"])

    assert {s.dimension for s in specs} == {"priority", "risk", "cost"}


def test_두_도메인을_고르면_다섯_워커가_돈다():
    specs = registry.specs_for_domains(["project", "hcm"])
    assert len(specs) == 5


def test_아직_없는_도메인은_조용히_건너뛴다():
    # vacation 은 이제 워커가 있다(app/workers/vacation/vacation_worker.py —
    # app/tests/test_vacation_worker.py 참고) — 그래서 "아직 구현 안 된 도메인"
    # 예시로는 실존하지 않는 이름을 대신 쓴다.
    assert registry.specs_for_domains(["definitely_not_a_real_domain"]) == []
    assert registry.unsupported_domains(["project", "definitely_not_a_real_domain"]) == \
        ["definitely_not_a_real_domain"]


def test_meeting_도메인도_라우팅에_잡힌다():
    """담당자 3 에이전트를 어댑터로 붙였다."""
    specs = registry.specs_for_domains(["meeting"])

    assert [s.node_name for s in specs] == ["meeting.schedule"]
    assert specs[0].runner is not None  # 프롬프트가 아니라 자체 구현으로 돈다


def test_축_이름으로_워커를_찾을_수_있다():
    """Validator 가 특정 축만 재실행시킬 때 쓴다."""
    spec = registry.spec_by_dimension("workload")

    assert spec is not None
    assert spec.domain == "hcm"
    assert spec.node_name == "hcm.workload"


def test_모든_워커는_결과스키마와_프롬프트를_갖는다():
    for spec in registry.all_specs():
        assert spec.result_model is not None
        if spec.runner is not None:
            continue  # 자체 구현 워커는 프롬프트·도구를 쓰지 않는다
        assert spec.role.strip(), f"{spec.dimension} 역할 프롬프트 없음"
        assert spec.method.strip(), f"{spec.dimension} 판단절차 프롬프트 없음"
        # my_week 는 일부러 도구가 없다 — 컨텍스트가 곧 전부라 더 찾아 헤맬 곳이 없어야 한다.
        if spec.dimension != "my_week":
            assert spec.tools, f"{spec.dimension} 에 도구가 없음"


def test_그래프가_조립된다():
    """API 키 없이도 그래프 구성 자체는 성공해야 한다."""
    from app.engine_b.graph import build_analysis_core, build_engine_b_graph

    assert build_engine_b_graph() is not None
    assert build_analysis_core() is not None


# ─── 통합 결과 사후 검증 ──────────────────────────────────────────

def test_과거_날짜로_마감을_바꾸자는_제안은_잡는다(context_p001):
    result = SynthesisResult(
        proposed_changes=[
            ProposedChange(
                kind="todo_update",
                target="todo:103",
                after={"due_date": "2026-07-01"},
            )
        ]
    )
    violations = validate_synthesis(result, context_p001)

    assert "PAST_DATE" in {v.code for v in violations}


def test_목표일을_넘기는_마감_제안은_잡는다(context_p001):
    result = SynthesisResult(
        proposed_changes=[
            ProposedChange(
                kind="todo_update",
                target="todo:103",
                after={"due_date": "2026-12-01"},  # p001 목표일 2026-09-30
            )
        ]
    )
    violations = validate_synthesis(result, context_p001)

    assert "DEADLINE_EXCEEDED" in {v.code for v in violations}


def test_없는_담당자로_바꾸자는_제안은_잡는다(context_p001):
    result = SynthesisResult(
        proposed_changes=[
            ProposedChange(
                kind="assignment", target="todo:103", after={"assignee_id": 999}
            )
        ]
    )
    violations = validate_synthesis(result, context_p001)

    assert "UNKNOWN_USER" in {v.code for v in violations}


def test_DB변경_제안이_있어야_승인이_필요하다():
    """읽기 전용 분석은 HITL 게이트 없이 바로 나간다."""
    assert SynthesisResult().requires_approval is False
    assert (
        SynthesisResult(
            proposed_changes=[ProposedChange(kind="todo_update", target="todo:t1")]
        ).requires_approval
        is True
    )
