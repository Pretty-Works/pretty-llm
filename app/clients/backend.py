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
    if path == "/projects":
        return {
            "projects": [
                {"projectId": 3, "name": "그룹웨어 AI 고도화",
                 "startDate": "2026-06-01", "endDate": "2026-09-30", "canEdit": True},
                {"projectId": 7, "name": "검색 고도화",
                 "startDate": "2026-07-01", "endDate": "2026-12-31", "canEdit": True},
            ],
            "totalCount": 2, "truncated": False,
        }
    if path.endswith("/members"):
        return {
            "members": [
                {"userId": 2, "name": "김서준", "department": "프로젝트관리", "position": "팀장"},
                {"userId": 5, "name": "이하늘", "department": "백엔드개발", "position": "사원"},
                {"userId": 7, "name": "정우진", "department": "프론트개발", "position": "선임"},
            ],
            "totalCount": 3, "truncated": False,
        }
    if path.endswith("/meetings"):
        return {
            "meetings": [
                {"meetingId": 41, "title": "스프린트 리뷰 3차",
                 "meetingDate": "2026-07-29", "canEdit": True},
            ],
            "totalCount": 1, "truncated": False,
        }
    if path == "/leaves/balance":
        return {"year": 2026, "total": 15, "used": 3, "remaining": 12}
    if path == "/me":
        return {"userId": 5, "name": "이하늘", "department": "백엔드개발", "position": "사원"}
    return {"totalCount": 0, "truncated": False}


def _mock_write(path: str, body: bytes) -> Any:
    """쓰기 mock. 실제로 저장하지 않고 생성된 것처럼 id 만 돌려준다."""
    if path.endswith("/meetings"):
        return {"meetingId": 57}
    if path == "/tasks":
        return {"taskId": 88}
    if path == "/leaves":
        return {"leaveId": 12}
    if path == "/schedules":
        return {"scheduleId": 31}
    return {}


# 앱 전역 단일 인스턴스
backend = BackendClient()
