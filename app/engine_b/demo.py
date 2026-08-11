# app/engine_b/demo.py
"""Engine B 수동 실행기 (데모/디버깅용).

    uv run python -m app.engine_b.demo
    uv run python -m app.engine_b.demo "예산이 빠듯한데 뭐부터 해야 할까?" p002

라우팅 → 워커 병렬 → 검증 → 통합까지 한 번에 돌리고, 각 단계가 무엇을 했는지 출력한다.
백엔드가 안 떠 있어도 픽스처(app/tools/demo_data.py)로 끝까지 돈다.
"""

import asyncio
import sys

from app.engine_b.graph import run_analysis
from app.schemas.state import AnalysisRequest, UIContext

_DEFAULT_QUERY = "그룹웨어 리뉴얼 프로젝트 지금 위험한 거 있으면 알려주고, 인원 배치도 괜찮은지 봐줘"
_DEFAULT_PROJECT = 1001


def main() -> None:
    query = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_QUERY
    project_id = sys.argv[2] if len(sys.argv) > 2 else _DEFAULT_PROJECT

    state = asyncio.run(
        run_analysis(
            AnalysisRequest(
                query=query,
                user_id=1,
                ui_context=UIContext(screen="project_detail", project_id=project_id),
            )
        )
    )

    plan = state["plan"]
    print("\n" + "=" * 72)
    print(f"질문: {query}")
    print("=" * 72)

    print(f"\n[1] Analysis Router  mode={plan.mode}  domains={plan.domains}  focus={plan.focus}")
    print(f"    목표: {plan.objective}")
    print(f"    판단: {plan.reasoning}")

    context = state["context"]
    print(
        f"\n[2] Context Builder  프로젝트 {len(context.projects)}건 / "
        f"후보 {len(context.candidates)}명 / 휴가 {len(context.leaves)}건"
    )
    if context.missing:
        print(f"    미확보: {context.missing}")

    print(f"\n[3] Worker Layer  {len(state.get('worker_outputs', []))}개 실행")
    for output in sorted(state.get("worker_outputs", []), key=lambda o: o.dimension):
        mark = "!" if output.error else " "
        print(
            f"  {mark} {output.dimension:<10} conf={output.confidence:.2f} "
            f"도구 {output.tool_calls}회  시도 {output.attempt}회차"
        )
        print(f"      {output.reasoning[:160]}")

    validation = state.get("validation")
    if validation:
        print(f"\n[4] Validator  검사 {len(validation.checked)}건")
        for violation in validation.violations:
            print(f"    [{violation.severity}] {violation.code} ({violation.dimension}) {violation.message}")
        if not validation.violations:
            print("    위반 없음")

    result = state["result"]
    print(f"\n[5] Synthesis  conf={result.confidence:.2f}")
    print(f"    {result.headline}")
    print(f"    {result.summary}")

    if result.conflicts:
        print("\n  -- 상충과 해소 --")
        for conflict in result.conflicts:
            print(f"    · {'/'.join(conflict.between)}: {conflict.issue}")
            print(f"      -> {conflict.resolution}")

    if result.actions:
        print("\n  -- 권고 액션 --")
        for action in sorted(result.actions, key=lambda a: a.order):
            print(f"    {action.order}. {action.what}  (담당 {action.who or '미정'} / {action.when or '시점 미정'})")

    if result.proposed_changes:
        print(f"\n  -- DB 변경 제안 {len(result.proposed_changes)}건 (HITL 승인 필요) --")
        for change in result.proposed_changes:
            print(f"    · [{change.kind}] {change.target}: {change.summary}")
    else:
        print("\n  DB 변경 제안 없음 -> 읽기 전용, 승인 게이트 불필요")

    if result.unresolved_violations:
        print("\n  -- 해소되지 않은 규칙 위반 --")
        for violation in result.unresolved_violations:
            print(f"    · {violation.code}: {violation.message}")

    print("\n[trace]")
    for event in state.get("trace", []):
        print(f"  {event.node:<20} {event.message[:80]}")
    print()


if __name__ == "__main__":
    main()
