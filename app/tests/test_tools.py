"""
도구 층 스모크 테스트 — 카탈로그 22종 전수 (mock 백엔드)

가장 중요한 검증은 여전히 하나다 — **승인 시점과 실행 시점의 요청 바디가
바이트 단위로 같은가** (다르면 AGENT_015).

실행:  uv run python -m app.tests.test_tools
"""

from __future__ import annotations

import asyncio

from langchain.tools import ToolRuntime

from app.clients.backend import canonical_json
from app.tools.ask_user import ask_user
from app.tools.expense_tool import budget_summary, expense_create, expense_list
from app.tools.leave_tool import leave_balance, leave_create, leave_list, leave_update
from app.tools.meeting_tool import meeting_create, meeting_detail, meeting_list
from app.tools.milestone_tool import milestone_list, milestone_toggle_status
from app.tools.navigate import fill_form, navigate
from app.tools.project_tool import project_members, project_search
from app.tools.registry import (WRITE_TOOLS, RunContext, build_request,
                                catalog_name, is_write)
from app.tools.schedule_tool import schedule_create, schedule_list, schedule_update
from app.tools.task_tool import task_create, task_list, task_toggle_status
from app.tools.user_tool import user_me, user_search

READ = [user_me, user_search, project_search, project_members, milestone_list,
        task_list, meeting_list, meeting_detail, budget_summary, expense_list,
        schedule_list, leave_balance, leave_list]
WRITE = [meeting_create, task_create, task_toggle_status, schedule_create,
         schedule_update, leave_create, leave_update, expense_create,
         milestone_toggle_status]
ETC = [ask_user, navigate, fill_form]

MEETING_ARGS = {
    "projectId": 3, "title": "스프린트 리뷰", "meetingDate": "2026-08-05",
    "location": None, "attendeeIds": [2, 7], "purpose": "진행 상황 공유",
    "content": "백엔드 API 68% 완료", "followUp": None, "recording": None,
}


def _runtime(ctx: RunContext) -> ToolRuntime:
    return ToolRuntime(state={}, context=ctx, config={},
                       stream_writer=lambda _: None, tool_call_id="tc_test", store=None)


def test_runtime_hidden() -> None:
    """runtime 은 주입값 — 어느 도구에서도 LLM 스키마에 노출되면 안 된다."""
    for t in READ + WRITE + ETC:
        props = t.tool_call_schema.model_json_schema().get("properties", {})
        assert "runtime" not in props, f"{t.name} 스키마에 runtime 노출"


def test_write_args_all_required() -> None:
    """쓰기 도구 9종 전부 — 선택 인자 금지 (생략되면 승인/실행 params 가 어긋난다)."""
    for t in WRITE:
        schema = t.tool_call_schema.model_json_schema()
        assert set(schema["required"]) == set(schema["properties"]), \
            f"{t.name} 에 선택 인자가 있음"


def test_catalog_coverage() -> None:
    """registry 의 쓰기 명세와 이 파일이 다루는 도구 9종(app/tools/*)의 관계 확인.

    ★ registry.WRITE_TOOLS는 app/tools/* 뿐 아니라 엔진B의 replan_save/
    replan_apply(app/engine_b/replan_tools.py)도 담고 있다 — 승인 시점/실행
    시점 바이트가 같아야 한다는 build_request() 규칙이 두 엔진 다 필요해서
    registry 하나로 모았기 때문이다(모듈 docstring 참고). 그 둘은 이 파일이
    import 하지 않으므로(엔진B 쪽에서 따로 검증할 몫) 1:1이 아니라 "이 파일의
    9종은 전부 registry에 있고, registry의 나머지는 replan 2종뿐"으로 확인한다."""
    tool_names = {t.name for t in WRITE}
    assert tool_names <= set(WRITE_TOOLS), f"registry에 없는 도구: {tool_names - set(WRITE_TOOLS)}"
    extra = set(WRITE_TOOLS) - tool_names
    assert extra == {"replan_save", "replan_apply"}, f"예상 밖 registry 항목: {extra}"
    assert catalog_name("meeting_create") == "meeting.create"
    assert catalog_name("milestone_toggle_status") == "milestone.toggleStatus"
    assert is_write("leave_update") and not is_write("leave_balance")


async def test_read_tools() -> None:
    """조회 13종 전부 mock 관통 — 응답 파싱이 명세 형태와 맞는지 확인."""
    rt = _runtime(RunContext(run_id="run_test"))
    checks = [
        (user_me, {}, "오늘: 2026-08-05"),
        (user_search, {"keyword": "김서준"}, "[2] 김서준"),
        (project_search, {"keyword": "그룹웨어"}, "[3] 그룹웨어 AI 고도화"),
        (project_members, {"projectId": 3}, "★본인"),
        (milestone_list, {"projectId": 3}, "베타 오픈"),
        (task_list, {"projectId": None, "weekOffset": 0}, "이월"),
        (meeting_list, {"projectId": 3}, "[41]"),
        (meeting_detail, {"projectId": 3, "meetingId": 41}, "후속 조치"),
        (budget_summary, {"projectId": 3}, "집행률 62%"),
        (expense_list, {"projectId": 3, "sort": "AMOUNT_DESC"}, "젯브레인"),
        (schedule_list, {"fromDate": "2026-08-03", "toDate": "2026-08-16"}, "휴가"),
        (leave_balance, {}, "잔여 12일"),
        (leave_list, {"fromDate": "2026-08-01", "toDate": "2026-08-31"}, "[31]"),
    ]
    for t, args, expect in checks:
        out = await t.coroutine(**args, runtime=rt)
        assert expect in out, f"{t.name}: {expect!r} 없음 → {out[:120]!r}"


async def test_params_bytes_identical(captured: dict) -> None:
    """★ 핵심 — 승인 시점 바이트 == 실행 시점 바이트 (모든 쓰기 도구의 공통 구조)."""
    _, path, params = build_request("meeting_create", MEETING_ARGS)
    approval_bytes = canonical_json(params)
    assert path == "/projects/3/meetings", path

    rt = _runtime(RunContext(run_id="run_test", approval_token="apv_test"))
    out = await meeting_create.coroutine(**MEETING_ARGS, runtime=rt)

    assert captured["body"] == approval_bytes, "바이트 불일치 → AGENT_015"
    assert captured["approval_token"] == "apv_test"
    assert "meetingId=57" in out, out


async def test_write_smoke(captured: dict) -> None:
    """쓰기 8종(회의록 제외) mock 관통 — 경로·응답 파싱 확인."""
    rt = _runtime(RunContext(run_id="run_test", approval_token="apv_test"))
    cases = [
        (task_create, {"tasks": [{"content": "명세 정리", "dueDate": "2026-08-07",
                                  "projectId": 3}]}, "/tasks", "1건"),
        (task_toggle_status, {"taskId": 58, "completed": True}, "/tasks/58/status", "완료"),
        (schedule_create, {"title": "팀미팅", "startAt": "2026-08-11T14:00:00",
                           "endAt": "2026-08-11T15:00:00", "type": "MEETING",
                           "allDay": False, "participantUserIds": [2]}, "/schedules", "scheduleId=62"),
        (schedule_update, {"scheduleId": 61, "title": None, "startAt": "2026-08-06T15:00:00",
                           "endAt": "2026-08-06T15:30:00", "type": None, "allDay": None,
                           "participantUserIds": None}, "/schedules/61", "수정"),
        (leave_create, {"leaveType": "ANNUAL", "startDate": "2026-08-11",
                        "endDate": "2026-08-12", "reason": None}, "/leaves", "잔여 10일"),
        (leave_update, {"leaveId": 31, "leaveType": None, "startDate": None,
                        "endDate": None, "reason": ""}, "/leaves/31", "수정"),
        (expense_create, {"projectId": 3, "expenseDate": "2026-08-01", "category": "MEAL",
                          "merchant": "한경식당", "purpose": "회식", "amount": 120000},
         "/projects/3/expenses", "120,000원"),
        (milestone_toggle_status, {"projectId": 3, "milestoneId": 12, "completed": True},
         "/projects/3/milestones/12/status", "100%"),
    ]
    for t, args, want_path, expect in cases:
        out = await t.coroutine(**args, runtime=rt)
        assert captured["path"] == want_path, f"{t.name}: 경로 {captured['path']}"
        assert expect in out, f"{t.name}: {expect!r} 없음 → {out[:120]!r}"


async def test_params_canonical_priority(captured: dict) -> None:
    given = b'{"from":"backend"}'
    rt = _runtime(RunContext(run_id="run_test", approval_token="apv_test",
                             params_canonical=given))
    await meeting_create.coroutine(**MEETING_ARGS, runtime=rt)
    assert captured["body"] == given, "paramsCanonical 이 무시됨"


async def main() -> None:
    from app.clients import backend as backend_mod

    captured: dict = {}
    original = backend_mod.backend.write

    async def spy(method, path, run_id, approval_token, body):
        captured.update(method=method, path=path, run_id=run_id,
                        approval_token=approval_token, body=body)
        return await original(method, path, run_id=run_id,
                              approval_token=approval_token, body=body)

    backend_mod.backend.write = spy  # type: ignore[method-assign]

    test_runtime_hidden();           print(f"✅ runtime 숨김 ({len(READ + WRITE + ETC)}종)")
    test_write_args_all_required();  print("✅ 쓰기 9종 인자 전부 required")
    test_catalog_coverage();         print("✅ registry 9종 ↔ 도구 1:1 + 이름 매핑")
    await test_read_tools();         print("✅ 조회 13종 mock 관통")
    await test_params_bytes_identical(captured)
    print("✅ 승인 바이트 == 실행 바이트")
    await test_write_smoke(captured)
    print("✅ 쓰기 8종 mock 관통 (경로·응답)")
    await test_params_canonical_priority(captured)
    print("✅ paramsCanonical 우선 적용")

    print("\n전부 통과 — 도구 22종 + ask_user·navigate·fill_form")


if __name__ == "__main__":
    asyncio.run(main())
