# app/engine_b/validator.py
"""Validator — 코드로 하는 규칙 검증.

LLM 에게 "규칙을 지켰는지 확인해줘"라고 다시 묻지 않는다. 그건 같은 실수를 두 번 하는 길이다.
날짜·예산·가용성처럼 **참/거짓이 확정되는 것**은 전부 여기서 코드로 판정한다.

위반이 error 로 잡히면 해당 축의 워커만 수정 지시(fix_hint)와 함께 다시 돌린다.
warning 은 루프를 돌리지 않고 최종 결과에 그대로 붙여 사용자에게 보여준다.
"""

from typing import Any, Callable

from app.config import get_settings
from app.schemas.state import (
    AnalysisContext,
    AnalysisPlan,
    ScenarioSpec,
    ValidationReport,
    Violation,
    WorkerOutput,
)
from app.utils.logger import get_logger
from app.utils.parser import overlaps, parse_date

log = get_logger("engine_b.validator")

# skill_fit 은 스킬 데이터 없이 이력으로 추론하는 축이라 구조적으로 확신할 수 없다.
# 프롬프트로만 막으면 모델이 자주 넘기므로 코드로 상한을 강제한다.
SKILL_FIT_CONFIDENCE_CAP = 0.75

# 정수 지표는 정확히 일치해야 하고, load_index 만 반올림 오차를 허용한다.
_LOAD_INDEX_TOLERANCE = 0.05
_RISK_SCORE_TOLERANCE = 2


def validate(
    outputs: list[WorkerOutput],
    context: AnalysisContext,
    plan: AnalysisPlan | None = None,
    scenario: ScenarioSpec | None = None,
) -> ValidationReport:
    """워커 출력 전체를 검증한다."""
    report = ValidationReport()
    checks: dict[str, Callable[[WorkerOutput, AnalysisContext, ValidationReport], None]] = {
        "priority": _check_priority,
        "risk": _check_risk,
        "cost": _check_cost,
        "skill_fit": _check_skill_fit,
        "workload": _check_workload,
    }

    for output in outputs:
        _check_common(output, report)

        # 실패한 워커는 result 가 비어 있다. 그걸 규칙 위반으로 오인하면
        # (예: 예산 0원 ≠ 실제 예산) 살아날 수 없는 워커를 한도까지 재시도하게 된다.
        if output.error:
            continue

        check = checks.get(output.dimension)
        if check is None:
            continue
        try:
            check(output, context, report)
            report.checked.append(f"{output.scenario_id}/{output.dimension}")
        except Exception as exc:
            # 검증기 버그로 분석 전체가 죽지 않게 한다.
            log.error("검증 중 오류 (%s): %s", output.dimension, exc)
            report.violations.append(
                Violation(
                    code="VALIDATOR_ERROR",
                    severity="warning",
                    dimension=output.dimension,
                    scenario_id=output.scenario_id,
                    message=f"검증기 오류: {exc}",
                )
            )

    log.info(
        "검증 완료: 검사 %d건, error %d, warning %d",
        len(report.checked),
        len(report.errors),
        len(report.violations) - len(report.errors),
    )
    return report


# ─── 공통 ─────────────────────────────────────────────────────────

def _check_common(output: WorkerOutput, report: ValidationReport) -> None:
    settings = get_settings()

    if output.error:
        report.violations.append(
            Violation(
                code="WORKER_ERROR",
                severity="warning",
                dimension=output.dimension,
                scenario_id=output.scenario_id,
                message=f"{output.dimension} 워커가 실패했다: {output.error}",
                fix_hint="이 축의 결론은 신뢰할 수 없으니 통합 시 제외한다.",
            )
        )
        return

    if output.confidence < settings.low_confidence_threshold:
        report.violations.append(
            Violation(
                code="LOW_CONFIDENCE",
                severity="warning",
                dimension=output.dimension,
                scenario_id=output.scenario_id,
                message=f"{output.dimension} 확신도가 낮다 ({output.confidence:.2f}).",
                fix_hint="사용자에게 근거 부족을 함께 알린다.",
            )
        )

    if not output.evidence:
        report.violations.append(
            Violation(
                code="NO_EVIDENCE",
                severity="warning",
                dimension=output.dimension,
                scenario_id=output.scenario_id,
                message=f"{output.dimension} 결론에 근거가 하나도 없다.",
                fix_hint="주요 결론마다 evidence 를 남겨야 한다.",
            )
        )


def _add(
    report: ValidationReport,
    output: WorkerOutput,
    code: str,
    message: str,
    fix_hint: str,
    *,
    severity: str = "error",
    subject: str = "",
) -> None:
    report.violations.append(
        Violation(
            code=code,
            severity=severity,  # type: ignore[arg-type]
            dimension=output.dimension,
            scenario_id=output.scenario_id,
            subject=subject,
            message=message,
            fix_hint=fix_hint,
        )
    )


# ─── priority ─────────────────────────────────────────────────────

def _check_priority(
    output: WorkerOutput, context: AnalysisContext, report: ValidationReport
) -> None:
    known = {str(t.id): t for p in context.projects for t in p.todos}
    ranked = output.result.get("ranked") or []
    seen: set[str] = set()

    for item in ranked:
        task_id = str(item.get("task_id", ""))
        seen.add(task_id)
        todo = known.get(task_id)

        if todo is None:
            _add(
                report,
                output,
                "UNKNOWN_TASK",
                f"존재하지 않는 할 일을 순위에 넣었다: {task_id}",
                "컨텍스트에 있는 할 일 id 만 사용하라.",
                subject=task_id,
            )
            continue

        if todo.status in {"DONE", "CANCELED"}:
            _add(
                report,
                output,
                "CLOSED_TASK_RANKED",
                f"{task_id}({todo.title}) 는 {todo.status} 상태인데 우선순위에 들어 있다.",
                "DONE / CANCELED 는 대상에서 제외하라.",
                subject=task_id,
            )

        # 지연 여부는 날짜로 결정된다. 모델이 뒤집어 적으면 뒤 판단이 전부 어긋난다.
        expected_overdue = bool(todo.due_date and todo.due_date < context.as_of)
        if bool(item.get("is_overdue")) != expected_overdue:
            _add(
                report,
                output,
                "OVERDUE_FLAG_MISMATCH",
                f"{task_id} 의 지연 여부가 실제와 다르다 "
                f"(마감 {todo.due_date}, 오늘 {context.as_of}).",
                f"{task_id} 의 is_overdue 를 {expected_overdue} 로 고쳐라.",
                subject=task_id,
            )

        stated_due = parse_date(item.get("due_date"))
        if stated_due and todo.due_date and stated_due != todo.due_date:
            _add(
                report,
                output,
                "DATE_MISMATCH",
                f"{task_id} 의 마감일을 임의로 바꿨다 ({stated_due} ≠ {todo.due_date}).",
                "우선순위 축은 마감일을 바꾸지 않는다. 실제 값을 그대로 쓰라.",
                subject=task_id,
            )

        for blocked in item.get("blocks") or []:
            if str(blocked) not in known:
                _add(
                    report,
                    output,
                    "UNKNOWN_TASK",
                    f"{task_id} 가 막고 있다고 한 {blocked} 가 존재하지 않는다.",
                    "존재하는 할 일 id 만 blocks 에 넣어라.",
                    subject=str(blocked),
                    severity="warning",
                )

    # 열린 할 일이 누락되면 순위표 자체가 반쪽이 된다 (경고로만)
    missing = [
        str(t.id) for p in context.projects for t in p.open_todos if str(t.id) not in seen
    ]
    if missing:
        _add(
            report,
            output,
            "INCOMPLETE_RANKING",
            f"순위에서 빠진 열린 할 일이 있다: {', '.join(str(t) for t in missing[:8])}",
            "열려 있는 할 일은 전부 순위에 포함하라.",
            severity="warning",
        )


# ─── risk ─────────────────────────────────────────────────────────

def _check_risk(
    output: WorkerOutput, context: AnalysisContext, report: ValidationReport
) -> None:
    known_todos = context.known_todo_ids()
    known_users = context.known_user_ids()

    for item in output.result.get("risks") or []:
        likelihood = _as_int(item.get("likelihood"))
        impact = _as_int(item.get("impact"))
        score = _as_int(item.get("risk_score"))
        title = str(item.get("title", ""))

        for label, value in (("likelihood", likelihood), ("impact", impact)):
            if not 0 <= value <= 100:
                _add(
                    report,
                    output,
                    "SCORE_RANGE",
                    f"'{title}' 의 {label} 가 범위를 벗어났다 ({value}).",
                    f"{label} 는 0~100 사이여야 한다.",
                    subject=title,
                )

        expected = round(likelihood * impact / 100)
        if abs(score - expected) > _RISK_SCORE_TOLERANCE:
            _add(
                report,
                output,
                "RISK_SCORE_MISMATCH",
                f"'{title}' 의 risk_score 가 계산과 다르다 ({score} ≠ {expected}).",
                f"risk_score = round(likelihood * impact / 100) = {expected} 로 고쳐라.",
                subject=title,
            )

        subject = str(item.get("subject", ""))
        if subject.startswith("todo:") and subject[5:] not in known_todos:
            _add(
                report,
                output,
                "UNKNOWN_SUBJECT",
                f"'{title}' 이 지목한 {subject} 가 데이터에 없다.",
                "실제 존재하는 할 일/구성원만 지목하라.",
                subject=subject,
            )
        if subject.startswith("user:") and subject[5:] not in known_users:
            _add(
                report,
                output,
                "UNKNOWN_SUBJECT",
                f"'{title}' 이 지목한 {subject} 가 데이터에 없다.",
                "실제 존재하는 할 일/구성원만 지목하라.",
                subject=subject,
            )

        if not str(item.get("mitigation", "")).strip():
            _add(
                report,
                output,
                "NO_MITIGATION",
                f"'{title}' 에 완화책이 없다.",
                "위험마다 실행 가능한 완화책을 적어라.",
                subject=title,
                severity="warning",
            )

    overall = _as_int(output.result.get("overall_risk_score"))
    if not 0 <= overall <= 100:
        _add(
            report,
            output,
            "SCORE_RANGE",
            f"overall_risk_score 가 범위를 벗어났다 ({overall}).",
            "0~100 사이로 맞춰라.",
        )


# ─── cost ─────────────────────────────────────────────────────────

def _check_cost(
    output: WorkerOutput, context: AnalysisContext, report: ValidationReport
) -> None:
    result = output.result
    total = _as_int(result.get("budget_total"))
    spent = _as_int(result.get("spent"))
    committed = _as_int(result.get("committed"))
    remaining = _as_int(result.get("remaining"))

    # 컨텍스트에 예산이 있으면 숫자를 그대로 썼는지 대조한다.
    budget = context.budgets[0] if context.budgets else None
    if budget:
        for label, stated, actual in (
            ("budget_total", total, budget.total),
            ("spent", spent, budget.spent),
            ("committed", committed, budget.committed),
        ):
            if stated != actual:
                _add(
                    report,
                    output,
                    "BUDGET_FIGURE_MISMATCH",
                    f"{label} 가 실제 값과 다르다 ({stated:,} ≠ {actual:,}).",
                    f"{label} 를 {actual:,} 로 고쳐라. 예산 수치는 지어내지 않는다.",
                    subject=label,
                )

    expected_remaining = total - spent - committed
    if remaining != expected_remaining:
        _add(
            report,
            output,
            "BUDGET_ARITHMETIC",
            f"잔액 계산이 맞지 않는다 ({remaining:,} ≠ {expected_remaining:,}).",
            f"잔액 = 총액 - 집행 - 결재중 = {expected_remaining:,} 로 고쳐라.",
        )

    overrun = _as_int(result.get("overrun_amount"))
    if overrun < 0:
        _add(
            report,
            output,
            "BUDGET_ARITHMETIC",
            f"overrun_amount 가 음수다 ({overrun:,}).",
            "초과가 없으면 0 으로 둔다.",
        )

    on_track = bool(result.get("on_track", True))
    if on_track and (overrun > 0 or expected_remaining < 0):
        _add(
            report,
            output,
            "BUDGET_EXCEEDED",
            f"초과 예상액이 {overrun:,}원인데 on_track 이 true 다.",
            "초과가 예상되면 on_track 을 false 로 두고 대응책을 levers 에 적어라.",
        )

    for lever in result.get("levers") or []:
        if not str(lever.get("tradeoff", "")).strip():
            _add(
                report,
                output,
                "LEVER_WITHOUT_TRADEOFF",
                f"절감안 '{lever.get('action')}' 에 포기할 것이 적혀 있지 않다.",
                "절감 수단마다 무엇을 포기하는지 tradeoff 에 적어라.",
                subject=str(lever.get("action", "")),
            )
        saving = _as_int(lever.get("expected_saving"))
        if total and saving > total:
            _add(
                report,
                output,
                "IMPLAUSIBLE_SAVING",
                f"절감액({saving:,})이 총예산({total:,})보다 크다.",
                "절감액을 실제 지출 항목 범위 안에서 다시 산정하라.",
                subject=str(lever.get("action", "")),
            )


# ─── skill_fit ────────────────────────────────────────────────────

def _check_skill_fit(
    output: WorkerOutput, context: AnalysisContext, report: ValidationReport
) -> None:
    known_users = context.known_user_ids()
    known_todos = context.known_todo_ids()

    # 구조적 상한: 추론 기반 축이 확신을 높게 주면 통합 단계가 잘못된 무게를 싣는다.
    if output.confidence > SKILL_FIT_CONFIDENCE_CAP:
        _add(
            report,
            output,
            "CONFIDENCE_CAP_EXCEEDED",
            f"적합도 확신도가 상한을 넘었다 ({output.confidence:.2f} > {SKILL_FIT_CONFIDENCE_CAP}).",
            f"스킬 데이터가 없는 추론이므로 confidence 를 {SKILL_FIT_CONFIDENCE_CAP} 이하로 낮춰라.",
        )

    for assignment in output.result.get("assignments") or []:
        target = str(assignment.get("target", ""))
        if target.startswith("todo:") and target[5:] not in known_todos:
            _add(
                report,
                output,
                "UNKNOWN_TASK",
                f"존재하지 않는 할 일에 인원을 배정했다: {target}",
                "컨텍스트에 있는 할 일만 대상으로 삼아라.",
                subject=target,
            )

        # 순위가 없는 구조라 '1순위/대안' 검사는 하지 않는다 (2026-08-11 재설계).
        for candidate in assignment.get("matches") or []:
            _check_candidate(output, context, report, candidate, target, known_users)


def _check_candidate(
    output: WorkerOutput,
    context: AnalysisContext,
    report: ValidationReport,
    candidate: dict[str, Any],
    target: str,
    known_users: set[str],
) -> None:
    user_id = str(candidate.get("user_id", ""))
    if not user_id:
        return

    if user_id not in known_users:
        _add(
            report,
            output,
            "UNKNOWN_USER",
            f"존재하지 않는 구성원을 추천했다: {user_id} ({target})",
            "컨텍스트의 구성원 목록에 있는 user_id 만 추천하라.",
            subject=user_id,
        )
        return

    # 부재는 배제 사유가 아니라 밝혀야 할 사실이다 — note 에 안 적혀 있으면 짚는다.
    conflict = _leave_conflict(context, user_id)
    if conflict and conflict not in str(candidate.get("note", "")):
        _add(
            report,
            output,
            "MEMBER_UNAVAILABLE",
            f"{user_id} 는 {conflict} 기간에 승인된 휴가가 있는데 note 에 없다.",
            f"{user_id} 의 note 에 {conflict} 부재를 적어라. 배제하라는 뜻이 아니라 밝히라는 뜻이다.",
            severity="warning",
            subject=user_id,
        )

    if not str(candidate.get("basis", "")).strip():
        _add(
            report,
            output,
            "NO_BASIS",
            f"{user_id} 를 제시한 근거가 비어 있다.",
            "이 프로젝트에서의 역할과 처리한 할 일 중 무엇을 봤는지 basis 에 적어라.",
            subject=user_id,
        )


def _leave_conflict(context: AnalysisContext, user_id: str) -> str | None:
    """분석 기간과 겹치는 승인 휴가가 있으면 그 기간 문자열을 돌려준다.

    user_id 는 호출부가 str() 로 정규화한 라벨이라 비교 전에 맞춰준다.
    """
    if not (context.window_from and context.window_to):
        return None
    for leave in context.leaves:
        if str(leave.user_id) != user_id or leave.status != "APPROVED":
            continue
        if overlaps(
            leave.start_date, leave.end_date, context.window_from, context.window_to
        ):
            return f"{leave.start_date}~{leave.end_date}"
    return None


# ─── workload ─────────────────────────────────────────────────────

def _check_workload(
    output: WorkerOutput, context: AnalysisContext, report: ValidationReport
) -> None:
    # 아래 비교가 문자열 라벨 기준이라 키도 문자열로 맞춘다
    actuals = {str(w["user_id"]): w for w in context.workloads}
    known_users = context.known_user_ids()

    for member in output.result.get("members") or []:
        user_id = str(member.get("user_id", ""))
        if user_id not in known_users:
            _add(
                report,
                output,
                "UNKNOWN_USER",
                f"존재하지 않는 구성원의 부하를 보고했다: {user_id}",
                "컨텍스트에 있는 구성원만 다뤄라.",
                subject=user_id,
            )
            continue

        actual = actuals.get(user_id)
        if not actual:
            continue

        # 코드가 센 값을 모델이 바꿔 적으면 이후 판단이 전부 어긋난다.
        for label in (
            "open_todo_count",
            "overdue_count",
            "due_in_window_count",
            "approved_leave_days",
            "available_days",
        ):
            stated = _as_int(member.get(label))
            if stated != int(actual.get(label) or 0):
                _add(
                    report,
                    output,
                    "METRIC_MISMATCH",
                    f"{user_id} 의 {label} 가 계산값과 다르다 "
                    f"({stated} ≠ {actual.get(label)}).",
                    f"{user_id} 의 {label} 를 {actual.get(label)} 로 고쳐라. "
                    "부하 지표는 주어진 값을 그대로 인용해야 한다.",
                    subject=user_id,
                )

        stated_index = member.get("load_index")
        actual_index = actual.get("load_index")
        if stated_index is not None and actual_index is not None:
            if abs(float(stated_index) - float(actual_index)) > _LOAD_INDEX_TOLERANCE:
                _add(
                    report,
                    output,
                    "METRIC_MISMATCH",
                    f"{user_id} 의 load_index 가 계산값과 다르다 "
                    f"({stated_index} ≠ {actual_index}).",
                    f"{user_id} 의 load_index 를 {actual_index} 로 고쳐라.",
                    subject=user_id,
                )

    for hint in output.result.get("rebalance_hints") or []:
        for candidate_id in hint.get("to_user_candidates") or []:
            candidate_id = str(candidate_id)
            if candidate_id not in known_users:
                _add(
                    report,
                    output,
                    "UNKNOWN_USER",
                    f"재배분 후보에 존재하지 않는 구성원이 있다: {candidate_id}",
                    "실제 구성원만 후보로 제시하라.",
                    subject=candidate_id,
                )
                continue
            conflict = _leave_conflict(context, candidate_id)
            if conflict:
                _add(
                    report,
                    output,
                    "MEMBER_UNAVAILABLE",
                    f"재배분 후보 {candidate_id} 는 {conflict} 에 승인된 휴가가 있다.",
                    f"{candidate_id} 를 후보에서 빼거나, 휴가 기간을 피한 일정으로 제안하라.",
                    subject=candidate_id,
                )


# ─── 통합 결과 사후 점검 (루프는 돌리지 않고 경고만 붙인다) ────────

def validate_synthesis(result, context: AnalysisContext) -> list[Violation]:
    """Synthesis 가 만든 DB 변경 제안이 규칙을 어기지 않는지 마지막으로 훑는다."""
    violations: list[Violation] = []
    known_users = context.known_user_ids()
    known_todos = context.known_todo_ids()
    due_dates = {p.id: p.due_date for p in context.projects}

    for change in result.proposed_changes:
        target = change.target or ""
        if target.startswith("todo:") and target[5:] not in known_todos:
            violations.append(
                Violation(
                    code="UNKNOWN_TASK",
                    severity="error",
                    subject=target,
                    message=f"변경 제안 대상이 존재하지 않는다: {target}",
                    fix_hint="실제 할 일 id 로 제안해야 반영할 수 있다.",
                )
            )

        after = change.after or {}
        assignee = str(after.get("assignee_id", ""))
        if assignee and assignee not in known_users:
            violations.append(
                Violation(
                    code="UNKNOWN_USER",
                    severity="error",
                    subject=assignee,
                    message=f"변경 제안의 담당자가 존재하지 않는다: {assignee}",
                    fix_hint="실제 구성원으로 지정하라.",
                )
            )

        new_due = parse_date(after.get("due_date"))
        if new_due:
            if new_due < context.as_of:
                violations.append(
                    Violation(
                        code="PAST_DATE",
                        severity="error",
                        subject=target,
                        message=f"{target} 의 새 마감일이 과거다 ({new_due}).",
                        fix_hint=f"{context.as_of} 이후로 잡아라.",
                    )
                )
            project_due = next((d for d in due_dates.values() if d), None)
            if project_due and new_due > project_due:
                violations.append(
                    Violation(
                        code="DEADLINE_EXCEEDED",
                        severity="error",
                        subject=target,
                        message=f"{target} 의 새 마감일({new_due})이 프로젝트 목표일({project_due})을 넘는다.",
                        fix_hint="목표일 안으로 잡거나, 목표일 변경도 함께 제안하라.",
                    )
                )

        start, end = parse_date(after.get("start_date")), parse_date(after.get("end_date"))
        if start and end and start > end:
            violations.append(
                Violation(
                    code="DATE_ORDER",
                    severity="error",
                    subject=target,
                    message=f"{target} 의 시작일이 종료일보다 늦다 ({start} > {end}).",
                    fix_hint="시작일 ≤ 종료일이 되도록 고쳐라.",
                )
            )

    return violations


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0
