# app/tools/hr_tool.py
"""인력(hcm) 도메인 읽기 툴 — 가용성만 본다.

★ 2026-08-11 재설계. 이 파일에서 다루는 범위는 하나로 좁혔다:
  **요청자가 화면에서 이미 볼 수 있는 것만 본다** (권한 동등성).

  프로젝트 팀원끼리는 캘린더에서 서로의 휴가·일정을 본다. 그건 조회한다.
  전사 명부·타인의 다른 프로젝트 이력·잔여연차는 화면으로 갈 수 없는 곳이라 조회하지 않는다.
  BE 도 그 선을 그어놨다 (`/users` keyword 필수, `/leaves/balance` 본인 한정).

  그래서 다음 툴을 제거했다 — 픽스처를 실데이터로 바꾼 게 아니라 기능을 접은 것이다.
    list_department_members  : 전사/부서 명부. BE 가 의도적으로 막은 경로
    get_user_project_history : 타인 경력 정보
    get_leave_balance        : 타인 근태 정보
    list_user_tasks          : 프로젝트 밖 타인 할 일

픽스처 폴백도 없앴다. 조회 실패는 빈 결과이고, 호출부가 `missing` 에 남긴다.
가짜 사람을 만들어 답을 채우느니 "확인 못 했다"가 낫다.
"""

import json
from datetime import date
from typing import Any

from langchain_core.tools import tool

from app.clients.backend import backend
from app.common.run_context import current_run_id
from app.config import get_settings
from app.utils.logger import get_logger
from app.utils.parser import parse_date

log = get_logger("tools.hr")

# 내부도구가 한 번에 받는 대상 인원 상한 (BE AgentCalendarToolService.MAX_TARGET_USERS)
_MAX_TARGET_USERS = 20


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


async def _get(path: str, **params: Any) -> Any | None:
    """실백엔드 조회. mock 모드·run_id 부재·실패면 None — 호출부가 '확인 못 함'으로 처리한다."""
    settings = get_settings()
    if settings.uses_fixtures:
        log.warning("픽스처 모드라 내부도구를 호출하지 않는다: %s (MOCK_BACKEND 확인)", path)
        return None
    run_id = current_run_id.get()
    if not run_id:
        log.warning("run_id 없이 내부도구 호출: %s — 조회 생략", path)
        return None
    try:
        return await backend.get(path, run_id, **params)
    except Exception as exc:  # 조회 실패로 분석을 죽이지는 않되, 값을 지어내지도 않는다
        log.warning("backend GET %s 실패 -> 빈 결과: %s", path, exc)
        return None


# ─── 조회 툴 (워커가 자율 호출) ───────────────────────────────────

@tool
async def find_user(name: str) -> str:
    """이름으로 구성원(부서·직책)을 조회한다. 질문에 나온 사람을 특정할 때만 쓴다."""
    user = await fetch_user(name=name)
    if not user:
        return _json({"error": "해당 구성원을 찾지 못했습니다.", "name": name})
    return _json(user)


@tool
async def list_member_leaves(user_ids: list[int], date_from: str = "", date_to: str = "") -> str:
    """프로젝트 참여자의 승인된 휴가를 조회한다. (YYYY-MM-DD)

    누가 언제 자리에 없는지만 본다. 휴가 사유는 BE 가 마스킹하므로 오지 않는다.
    """
    start, end = parse_date(date_from), parse_date(date_to)
    if not (start and end):
        return _json({"error": "date_from · date_to 가 모두 필요합니다."})
    leaves = await fetch_leaves(user_ids, start, end)
    return _json({"count": len(leaves), "leaves": leaves})


# ─── 비-툴 진입점 (Context Builder 가 직접 호출) ──────────────────

async def fetch_requester() -> dict | None:
    """요청자 본인. user.me 는 본인 스코프라 안전하고, 유일하게 정확한 신원 출처다."""
    raw = await _get("/me")
    if raw is None:
        return None
    return {"id": raw.get("userId"), "name": raw.get("name"),
            "department": raw.get("department"), "position": raw.get("position")}


async def fetch_user(name: str) -> dict | None:
    """이름 검색(user.search). id 로 남의 프로필을 여는 경로는 두지 않는다."""
    raw = await _get("/users", keyword=name)
    if raw is None:
        return None
    hits = raw.get("users", [])
    if not hits:
        return None
    u = hits[0]
    return {"id": u.get("userId"), "name": u.get("name"),
            "department": u.get("department"), "position": u.get("position")}


async def fetch_leaves(
    user_ids: list[int], date_from: date, date_to: date
) -> list[dict]:
    """승인된 휴가. 내부도구가 userIds 를 한 번에 받으므로 인원별로 나눠 부르지 않는다."""
    targets = _capped(user_ids)
    if not targets:
        return []
    raw = await _get("/leaves", **{"from": date_from.isoformat(), "to": date_to.isoformat(),
                                   "userIds": targets})
    if raw is None:
        return []
    return [
        {"id": leave.get("leaveId"), "user_id": leave.get("userId"),
         "user_name": leave.get("userName"), "leave_type": leave.get("leaveType") or "연차",
         "start_date": leave.get("startDate"), "end_date": leave.get("endDate")}
        for leave in raw.get("leaves", [])
    ]


async def fetch_schedules(
    user_ids: list[int], date_from: date, date_to: date
) -> list[dict]:
    """일정. 회의 시간 집계용이라 휴가(isLeave)는 제외한다 — 휴가는 fetch_leaves 가 센다."""
    targets = _capped(user_ids)
    if not targets:
        return []
    raw = await _get("/schedules", **{"from": date_from.isoformat(), "to": date_to.isoformat(),
                                      "userIds": targets})
    if raw is None:
        return []
    return [
        {"id": s.get("scheduleId"), "title": s.get("title"), "start_at": s.get("startAt"),
         "end_at": s.get("endAt"), "all_day": bool(s.get("allDay")),
         "participant_names": s.get("participantNames") or []}
        for s in raw.get("schedules", []) if not s.get("isLeave")
    ]


# ─── 본인 스코프 (me 도메인) ──────────────────────────────────────
#
# 아래 셋은 전부 요청자 본인 것만 돌려주는 내부도구다. userIds 를 안 싣는 게 핵심 —
# 그러면 BE 가 X-Run-Id 로 역산한 본인으로 스코프를 고정한다.

async def fetch_my_tasks(week_offset: int = 0) -> dict | None:
    """내 이번 주 할 일. projectId 를 안 주면 task.list 가 MINE 스코프로 답한다."""
    raw = await _get("/tasks", weekOffset=week_offset)
    if raw is None:
        return None
    return {
        "week_start": raw.get("weekStart"),
        "week_end": raw.get("weekEnd"),
        "tasks": [
            {"id": t.get("taskId"), "title": t.get("content"), "due_date": t.get("dueDate"),
             "status": "DONE" if t.get("completed") else "TODO",
             "project_id": t.get("projectId"), "project_name": t.get("projectName"),
             "is_carry_over": bool(t.get("isCarryOver"))}
            for t in raw.get("tasks", [])
        ],
    }


async def fetch_my_schedules(date_from: date, date_to: date) -> list[dict]:
    """내 일정. userIds 를 싣지 않아 본인 참여분만 온다."""
    raw = await _get("/schedules", **{"from": date_from.isoformat(), "to": date_to.isoformat()})
    if raw is None:
        return []
    return [
        {"id": s.get("scheduleId"), "title": s.get("title"), "start_at": s.get("startAt"),
         "end_at": s.get("endAt"), "all_day": bool(s.get("allDay")),
         "type": s.get("type"), "is_leave": bool(s.get("isLeave"))}
        for s in raw.get("schedules", [])
    ]


async def fetch_my_leave_balance(year: int | None = None) -> dict | None:
    """내 잔여 연차. leave.balance 는 본인 것만 준다 — 남의 것은 조회 경로가 없다."""
    raw = await _get("/leaves/balance", **({"year": year} if year else {}))
    if raw is None:
        return None
    return {"granted": raw.get("grantedDays"), "used": raw.get("usedDays"),
            "remaining": raw.get("remainingDays")}


def _capped(user_ids: list[int]) -> list[int]:
    """대상 인원 상한. 넘으면 앞에서 자른다 — 호출부가 notes 에 남긴다."""
    unique = list(dict.fromkeys(uid for uid in user_ids if uid))
    if len(unique) > _MAX_TARGET_USERS:
        log.warning("대상 인원 %d명이 상한(%d)을 넘어 잘랐다", len(unique), _MAX_TARGET_USERS)
    return unique[:_MAX_TARGET_USERS]


HR_TOOLS = [find_user, list_member_leaves]
