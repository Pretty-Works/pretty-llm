"""
백엔드(Spring) 내부 도구 API 호출 — 단일 창구

규격: ⭐ 내부 도구 API 공동 규격 (FastAPI → Spring)
  경로   /api/internal/agent/**
  헤더   X-Internal-Api-Key · X-Run-Id  (+ 쓰기는 X-Approval-Token)
  응답   {errorCode, message, result}  →  result 를 한 겹 벗겨 반환
  타임아웃 connect 3s / read 20s

★ 설계원칙 1 — 인터페이스 고정, 속만 교체.
  도구는 get()/write() 만 부른다. mock↔실제 전환은 이 파일 안에서 끝난다.

★ userId 를 절대 싣지 않는다.
  X-Run-Id 로 백엔드가 역산한다. 우리가 보내면 무시되며, 보낼 수 있게 두면
  에이전트가 남을 사칭하는 경로가 생긴다.

★ 쓰기는 params 를 재직렬화하지 않는다.
  백엔드가 승인 시점에 params 를 정규화해 SHA-256 을 고정해 둔다. 우리가 dict 를
  다시 문자열로 만들면 유니코드 이스케이프·키 순서가 미묘하게 달라져 해시가 어긋나고
  AGENT_015 로 거부된다(명세가 "붙이는 동안 가장 자주 나는 에러"라고 경고한 지점).
  그래서 write() 는 dict 가 아니라 bytes 를 받아 content= 로 그대로 흘려보낸다.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.common.exceptions import (
    ApprovalRequiredError,
    BackendUnavailableError,
    BackendValidationError,
    ParamsMismatchError,
)
from app.config import settings

_INTERNAL_PREFIX = "/api/internal/agent"


def canonical_json(params: dict[str, Any]) -> bytes:
    """params → 백엔드 정규화 규칙에 맞춘 바이트.

    ⚠️ 폴백 전용이다. 백엔드가 resume 바디로 paramsCanonical 문자열을 주면
       그걸 그대로 쓰는 게 안전하다 (규격 v2 §5). 이 함수는 그 필드를 못 받았을 때만 쓴다.

    규칙(공동 규격 §4):
      · 키를 사전순 정렬
      · 공백·줄바꿈 없음
      · null 필드 포함 (생략과 명시적 null 을 구분)
      · 배열 순서는 의미가 있으므로 정렬하지 않음
    """
    return json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


class BackendClient:
    """내부 도구 API 호출은 전부 여기를 거친다."""

    def __init__(self) -> None:
        self._timeout = httpx.Timeout(
            connect=settings.backend_connect_timeout_s,
            read=settings.backend_read_timeout_s,
            write=settings.backend_read_timeout_s,
            pool=settings.backend_connect_timeout_s,
        )

    # ── 조회 (승인 불필요) ────────────────────────────────────
    async def get(self, path: str, run_id: str, **params: Any) -> Any:
        """GET 도구. 조회 승인은 2026-08-03 폐지 — 바로 호출한다.

        path 는 /api/internal/agent 를 뺀 나머지 (예: "/projects").
        """
        if settings.mock_backend:
            return _mock_get(path, params)

        return await self._request(
            "GET", path, run_id=run_id, query=params, body=None, approval_token=None
        )

    # ── 쓰기 (승인 토큰 필수) ──────────────────────────────────
    async def write(
        self,
        method: str,
        path: str,
        run_id: str,
        approval_token: str,
        body: bytes,
    ) -> Any:
        """POST/PATCH/PUT 도구. body 는 승인 때 고정된 바이트를 그대로 넘긴다.

        멱등키는 붙이지 않는다 — 백엔드가 approvalId 를 멱등 키로 쓴다(공동 규격 §4).
        """
        if settings.mock_backend:
            return _mock_write(path, body)

        return await self._request(
            method, path, run_id=run_id, query=None, body=body,
            approval_token=approval_token,
        )

    # ── 실제 HTTP ─────────────────────────────────────────────
    async def _request(
        self,
        method: str,
        path: str,
        *,
        run_id: str,
        query: dict[str, Any] | None,
        body: bytes | None,
        approval_token: str | None,
    ) -> Any:
        headers = {
            "X-Internal-Api-Key": settings.internal_api_key,
            "X-Run-Id": run_id,
        }
        if approval_token:
            headers["X-Approval-Token"] = approval_token
        if body is not None:
            headers["Content-Type"] = "application/json"

        url = f"{settings.backend_base_url}{_INTERNAL_PREFIX}{path}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                res = await client.request(
                    method, url, headers=headers, params=query, content=body
                )
        except httpx.TimeoutException as e:
            raise BackendUnavailableError(f"백엔드 응답 시간 초과: {path}") from e
        except httpx.HTTPError as e:
            raise BackendUnavailableError(f"백엔드에 연결하지 못했습니다: {path}") from e

        return self._unwrap(res, path)

    @staticmethod
    def _unwrap(res: httpx.Response, path: str) -> Any:
        """{errorCode, message, result} 봉투를 벗기고, 실패는 예외로 바꾼다.

        ⚠️ 성공 응답도 최상위가 아니라 result 안에 실제 데이터가 있다(공동 규격 §1).
        """
        try:
            payload = res.json()
        except ValueError:
            raise BackendUnavailableError(f"백엔드 응답이 JSON 이 아닙니다: {path}")

        if res.is_success:
            return payload.get("result")

        code = payload.get("errorCode")
        message = payload.get("message") or f"{res.status_code} {path}"

        # 에이전트가 다음 행동을 정할 수 있도록 실패를 종류별로 나눈다.
        if code == "AGENT_014":
            raise ApprovalRequiredError(message, backend_code=code)
        if code == "AGENT_015":
            raise ParamsMismatchError(message, backend_code=code)
        if res.status_code >= 500:
            raise BackendUnavailableError(message, backend_code=code)
        raise BackendValidationError(message, backend_code=code)


# ─────────────────────────────────────────────────────────────
# mock — 백엔드가 아직 안 도는 동안 개발/테스트용
#   settings.mock_backend = False 로 끄면 실제 호출로 바뀐다.
# ─────────────────────────────────────────────────────────────
def _mock_get(path: str, params: dict[str, Any]) -> Any:
    """조회 mock — 응답 형태는 노션 개별 명세(docs/tool_specs_read.md)와 동일하게 유지한다."""
    if path == "/me":
        return {"userId": 5, "name": "이하늘", "department": "백엔드개발", "position": "사원",
                "canCreateProject": True, "today": "2026-08-05", "todayDayOfWeek": "WEDNESDAY",
                "thisWeekStart": "2026-08-03", "thisWeekEnd": "2026-08-09"}
    if path == "/users":
        users = [
            {"userId": 2, "name": "김서준", "department": "프로젝트관리", "position": "팀장", "isMe": False},
            {"userId": 5, "name": "이하늘", "department": "백엔드개발", "position": "사원", "isMe": True},
            {"userId": 7, "name": "정우진", "department": "프론트개발", "position": "선임", "isMe": False},
            {"userId": 9, "name": "박지원", "department": "디자인", "position": "사원", "isMe": False},
        ]
        kw = params.get("keyword", "")
        hits = [u for u in users if kw in u["name"]]
        return {"users": hits, "totalCount": len(hits), "truncated": False}
    if path == "/projects":
        return {
            "projects": [
                {"projectId": 3, "name": "그룹웨어 AI 고도화", "status": "ONGOING",
                 "startDate": "2026-06-01", "targetDate": "2026-09-30", "myRole": "백엔드",
                 "isOwner": False, "targetBudget": 30000000, "isOpenForContent": True},
                {"projectId": 7, "name": "검색 고도화", "status": "ONGOING",
                 "startDate": "2026-07-01", "targetDate": "2026-12-31", "myRole": None,
                 "isOwner": True, "targetBudget": 0, "isOpenForContent": True},
            ],
            "totalCount": 2, "truncated": False,
        }
    if path.endswith("/members"):
        return {
            "projectId": 3,
            "members": [
                {"userId": 2, "name": "김서준", "department": "프로젝트관리", "position": "팀장",
                 "role": "PM", "isOwner": True, "isMe": False},
                {"userId": 5, "name": "이하늘", "department": "백엔드개발", "position": "사원",
                 "role": "백엔드", "isOwner": False, "isMe": True},
                {"userId": 7, "name": "정우진", "department": "프론트개발", "position": "선임",
                 "role": "프론트", "isOwner": False, "isMe": False},
            ],
            "totalCount": 3, "truncated": False,
        }
    if path.endswith("/milestones"):
        return {
            "projectId": 3,
            "milestones": [
                {"milestoneId": 11, "goal": "1차 스프린트 완료", "targetDate": "2026-07-15",
                 "completed": True, "completedAt": "2026-07-15T18:00:00", "isOverdue": False, "isNext": False},
                {"milestoneId": 12, "goal": "베타 오픈", "targetDate": "2026-08-20",
                 "completed": False, "completedAt": None, "isOverdue": False, "isNext": True},
            ],
            "summary": {"total": 2, "completed": 1, "completionRate": 50, "overdueCount": 0},
            "totalCount": 2, "truncated": False,
        }
    if path == "/tasks":
        return {
            "weekStart": "2026-08-03", "weekEnd": "2026-08-09",
            "scope": "PROJECT" if params.get("projectId") else "MINE",
            "tasks": [
                {"taskId": 58, "content": "API 명세 정리", "dueDate": "2026-08-07",
                 "completed": False, "isCarryOver": False, "canEdit": True,
                 "assigneeId": 5, "assigneeName": "이하늘", "projectId": 3, "projectName": "그룹웨어 AI 고도화"},
                {"taskId": 51, "content": "지난주 리뷰 반영", "dueDate": "2026-08-01",
                 "completed": False, "isCarryOver": True, "canEdit": True,
                 "assigneeId": 5, "assigneeName": "이하늘", "projectId": None, "projectName": None},
            ],
            "summary": {"total": 2, "completed": 0, "completionRate": 0, "carryOverCount": 1},
            "totalCount": 2, "truncated": False,
        }
    if "/meetings/" in path:                       # meeting.detail
        return {
            "meetingId": 41, "projectId": 3, "documentNo": "MTG-2026-0041",
            "title": "스프린트 리뷰 3차", "meetingDate": "2026-07-29",
            "location": "프로젝트룸 A", "purpose": "진행 점검",
            "content": "백엔드 API 60% 완료. 프론트 연동 지연 논의.",
            "followUp": "API 명세 문서화(이하늘), 연동 테스트 일정 확정(정우진)",
            "authorId": 2, "authorName": "김서준", "isMine": False, "canEdit": True,
            "attendees": [
                {"userId": 5, "name": "이하늘", "department": "백엔드개발"},
                {"userId": 7, "name": "정우진", "department": "프론트개발"},
            ],
        }
    if path.endswith("/meetings"):
        return {
            "projectId": 3,
            "meetings": [
                {"meetingId": 41, "title": "스프린트 리뷰 3차", "meetingDate": "2026-07-29",
                 "location": "프로젝트룸 A", "purpose": "진행 점검",
                 "authorName": "김서준", "attendeeNames": ["이하늘", "정우진"]},
            ],
            "totalCount": 1, "truncated": False,
        }
    if path.endswith("/budget"):
        return {
            "projectId": 3, "targetBudget": 30000000, "spentAmount": 18600000,
            "remainingAmount": 11400000, "executionRate": 62, "elapsedRate": 54, "expenseCount": 14,
            "byCategory": [
                {"category": "OUTSOURCING", "categoryLabel": "외주비", "amount": 12000000, "share": 65},
                {"category": "MEAL", "categoryLabel": "식비", "amount": 3600000, "share": 19},
                {"category": "SOFTWARE", "categoryLabel": "소프트웨어", "amount": 3000000, "share": 16},
            ],
        }
    if path.endswith("/expenses"):
        return {
            "expenses": [
                {"expenseId": 101, "expenseDate": "2026-07-28", "category": "MEAL",
                 "categoryLabel": "식비", "merchant": "한경식당", "purpose": "팀 회식",
                 "amount": 120000, "spenderName": "김서준", "canEdit": False},
                {"expenseId": 102, "expenseDate": "2026-08-01", "category": "SOFTWARE",
                 "categoryLabel": "소프트웨어", "merchant": "젯브레인", "purpose": "IDE 라이선스",
                 "amount": 350000, "spenderName": "이하늘", "canEdit": True},
            ],
            "totalCount": 2, "truncated": False,
        }
    if path == "/schedules":
        return {
            "from": params.get("from"), "to": params.get("to"),
            "schedules": [
                {"scheduleId": 61, "title": "주간 스크럼", "startAt": "2026-08-06T10:00:00",
                 "endAt": "2026-08-06T10:30:00", "allDay": False, "type": "MEETING",
                 "canEdit": True, "participantNames": ["이하늘", "김서준"], "isLeave": False},
                {"scheduleId": 74, "title": "연차", "startAt": "2026-08-11T00:00:00",
                 "endAt": "2026-08-11T23:59:59", "allDay": True, "type": "LEAVE",
                 "canEdit": True, "participantNames": ["이하늘"], "isLeave": True},
            ],
            "totalCount": 2, "truncated": False,
        }
    if path == "/leaves/balance":
        return {"year": int(params.get("year", 2026)), "grantedDays": 15, "usedDays": 3,
                "remainingDays": 12}
    if path == "/leaves":
        return {
            "leaves": [
                {"leaveId": 31, "leaveType": "ANNUAL", "startDate": "2026-08-11",
                 "endDate": "2026-08-11", "days": 1, "reason": "개인 사정",
                 "userId": 5, "userName": "이하늘", "canEdit": True},
            ],
            "totalCount": 1, "truncated": False,
        }
    return {"totalCount": 0, "truncated": False}


def _mock_write(path: str, body: bytes) -> Any:
    """쓰기 mock — 저장 없이 명세와 같은 모양의 응답을 돌려준다. body 를 읽어 에코한다."""
    data = json.loads(body) if body else {}

    if path.endswith("/meetings"):
        return {"meetingId": 57, "documentNo": "MTG-2026-0057", "projectId": 3,
                "title": data.get("title"), "meetingDate": data.get("meetingDate"),
                "createdAt": "2026-08-05T10:00:00"}
    if path == "/tasks":
        items = data.get("tasks", [])
        return {"createdCount": len(items),
                "tasks": [{"taskId": 91 + i, **t} for i, t in enumerate(items)]}
    if "/tasks/" in path and path.endswith("/status"):
        return {"taskId": int(path.split("/")[2]), "content": "API 명세 정리",
                "completed": data.get("completed"), "changed": True,
                "completedAt": "2026-08-05T11:00:00" if data.get("completed") else None}
    if path == "/schedules":
        n = len(data.get("participantUserIds") or [])
        return {"scheduleId": 62, "title": data.get("title"), "startAt": data.get("startAt"),
                "endAt": data.get("endAt"), "participantCount": n + 1}
    if path.startswith("/schedules/"):
        return {"scheduleId": int(path.split("/")[2]), "title": data.get("title") or "주간 스크럼",
                "startAt": data.get("startAt") or "2026-08-06T10:00:00",
                "endAt": data.get("endAt") or "2026-08-06T10:30:00",
                "type": data.get("type") or "MEETING", "participantCount": 2}
    if path == "/leaves":
        days = 1
        try:
            from datetime import date
            d1 = date.fromisoformat(data["startDate"]); d2 = date.fromisoformat(data["endDate"])
            days = (d2 - d1).days + 1
        except Exception:
            pass
        return {"leaveId": 32, "scheduleId": 75, "leaveType": data.get("leaveType"),
                "startDate": data.get("startDate"), "endDate": data.get("endDate"),
                "days": days, "remainingDaysAfter": 12 - days}
    if path.startswith("/leaves/"):
        return {"leaveId": int(path.split("/")[2]), "scheduleId": 74,
                "leaveType": data.get("leaveType") or "ANNUAL",
                "startDate": data.get("startDate") or "2026-08-11",
                "endDate": data.get("endDate") or "2026-08-11",
                "days": 1, "remainingDaysAfter": 11}
    if path.endswith("/expenses"):
        return {"expenseId": 104, "projectId": 3, "expenseDate": data.get("expenseDate"),
                "amount": data.get("amount"), "spentAmountAfter": 18600000 + (data.get("amount") or 0),
                "executionRateAfter": 63}
    if "/milestones/" in path and path.endswith("/status"):
        return {"milestoneId": int(path.split("/")[4]), "goal": "베타 오픈",
                "completed": data.get("completed"), "changed": True,
                "completedAt": "2026-08-05T12:00:00" if data.get("completed") else None,
                "completionRateAfter": 100 if data.get("completed") else 50}
    return {}


# 앱 전역 단일 인스턴스
backend = BackendClient()
