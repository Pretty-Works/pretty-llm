"""
쓰기 실패 정책 테스트 — 규격 §4 (4xx 재제안 · 5xx 재시도 · AGENT_015 재전송)

단위 (가짜 backend.write 로 오류 주입):
  ① 4xx(업무 규칙)  → WriteRejectedError, 재시도 없음, 사유·재승인 안내 포함
  ② 5xx 2회 후 성공 → 재시도로 성공 (호출 3회)
  ③ 5xx 3회        → "3회" 실패 안내
  ④ AGENT_015 1회 후 성공 → 원본 바이트 재전송으로 성공
  ⑤ AGENT_014      → 재승인 안내
  ⑥ 도구 수준: 예외가 아니라 문자열로 반환 (LLM 이 읽는 형태)

통합 (실 LLM): ⑦ 승인→실행에서 4xx → 실행이 죽지 않고 **새 approval_request** 로 재제안

실행:  uv run python -m app.tests.test_error_policy
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from app.common.exceptions import (ApprovalRequiredError, BackendUnavailableError,
                                   BackendValidationError, ParamsMismatchError,
                                   WriteRejectedError)
from app.tools.registry import RunContext
from app.tools.write_exec import execute_write

ARGS = {"taskId": 58, "completed": True}
CTX = RunContext(run_id="run_test", approval_token="apv_test")


class FakeWrite:
    """호출 순서대로 예외/성공을 재생하는 가짜 backend.write."""

    def __init__(self, script):
        self.script = list(script)      # 예외 인스턴스 또는 dict(성공 응답)
        self.calls = 0

    async def __call__(self, method, path, run_id, approval_token, body):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


async def _with_fake(script):
    from app.clients import backend as backend_mod
    fake = FakeWrite(script)
    original = backend_mod.backend.write
    backend_mod.backend.write = fake
    try:
        result = await execute_write("task_toggle_status", ARGS, CTX)
        return fake, result, None
    except WriteRejectedError as e:
        return fake, None, e
    finally:
        backend_mod.backend.write = original


async def unit_tests() -> None:
    ok = {"taskId": 58, "completed": True, "changed": True, "content": "x"}

    fake, res, err = await _with_fake([BackendValidationError("회의 일자가 기간 밖", backend_code="MEETING_008")])
    assert err and "MEETING_008" in str(err) and "다시 호출" in str(err), err
    assert fake.calls == 1, f"4xx 인데 재시도함: {fake.calls}회"
    print("✅ ① 4xx → 재시도 없이 사유+재승인 안내", flush=True)

    fake, res, err = await _with_fake([BackendUnavailableError("503"), BackendUnavailableError("503"), ok])
    assert res == ok and fake.calls == 3, (res, fake.calls)
    print("✅ ② 5xx 2회 → 3번째 재시도로 성공", flush=True)

    fake, res, err = await _with_fake([BackendUnavailableError("503")] * 3)
    assert err and "3회" in str(err), err
    assert fake.calls == 3
    print("✅ ③ 5xx 3회 → 중단 안내", flush=True)

    fake, res, err = await _with_fake([ParamsMismatchError("hash"), ok])
    assert res == ok and fake.calls == 2, (res, fake.calls)
    print("✅ ④ AGENT_015 → 원본 바이트 1회 재전송으로 성공", flush=True)

    fake, res, err = await _with_fake([ApprovalRequiredError("no token")])
    assert err and "AGENT_014" in str(err), err
    print("✅ ⑤ AGENT_014 → 재승인 안내", flush=True)

    # ⑥ 도구 수준 — 예외가 밖으로 새지 않고 문자열로
    from app.tools.task_tool import task_toggle_status
    from langchain.tools import ToolRuntime
    from app.clients import backend as backend_mod
    fake = FakeWrite([BackendValidationError("본인의 할일이 아님", backend_code="TASK_004")])
    original = backend_mod.backend.write
    backend_mod.backend.write = fake
    try:
        rt = ToolRuntime(state={}, context=CTX, config={},
                         stream_writer=lambda _: None, tool_call_id="tc", store=None)
        out = await task_toggle_status.coroutine(taskId=58, completed=True, runtime=rt)
        assert isinstance(out, str) and "TASK_004" in out, out
    finally:
        backend_mod.backend.write = original
    print("✅ ⑥ 도구는 예외 대신 안내문 반환 (LLM 이 읽는 형태)", flush=True)


async def integration_test() -> None:
    """⑦ 승인 후 실행에서 4xx → 실행이 안 죽고 새 approval_request 로 재제안."""
    from app.main import app
    from app.clients import backend as backend_mod

    original = backend_mod.backend.write
    state = {"failed_once": False}

    async def flaky(method, path, run_id, approval_token, body):
        if not state["failed_once"]:
            state["failed_once"] = True
            # 고칠 수 있는 사유여야 재제안이 성립한다 — "정우진(7)을 빼고 다시"
            raise BackendValidationError(
                "attendeeIds 에 참석 불가 사용자(userId 7, 정우진)가 포함되어 "
                "있습니다. 해당 인원을 제외하고 다시 저장하세요",
                backend_code="MEETING_002")
        return await original(method, path, run_id=run_id,
                              approval_token=approval_token, body=body)

    backend_mod.backend.write = flaky
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t",
                                     timeout=120) as client:

            async def collect(url, body):
                events, name = [], None
                async with client.stream("POST", url, json=body) as res:
                    async for line in res.aiter_lines():
                        if line.startswith("event: "):
                            name = line[7:]
                        elif line.startswith("data: "):
                            events.append((name, json.loads(line[6:])))
                return events

            run_id = f"run_{uuid.uuid4().hex[:8]}"
            events = await collect("/api/agent/runs", {
                "runId": run_id, "conversationId": 1,
                "goal": "그룹웨어 AI 고도화 프로젝트에 오늘(2026-08-05) 스프린트 리뷰 회의록 올려줘. "
                        "참석자 김서준·정우진, 목적 진행 공유, 내용 API 80% 완료.",
                "messages": [], "screenContext": {"screen": "HOME", "formState": {}},
                "requestSource": "WEB", "locale": "ko-KR"})
            hops = 0
            while events[-1][0] == "question" and hops < 3:
                events = await collect(f"/api/agent/runs/{run_id}/resume",
                                       {"questionId": 1, "selectedIds": [],
                                        "text": "그룹웨어 AI 고도화, 2026-08-05, 김서준·정우진"})
                hops += 1
            assert events[-1][0] == "approval_request", [n for n, _ in events]

            # 승인 → 도구 실행 → (주입된 4xx) → LLM 이 고쳐 재호출 → 새 승인 카드
            events = await collect(f"/api/agent/runs/{run_id}/resume",
                                   {"toolCallId": events[-1][1]["toolCallId"],
                                    "decision": "APPROVED", "approvalToken": "apv_t"})
            names = [n for n, _ in events]
            assert "error" not in names, f"실행이 죽음: {names}"
            assert events[-1][0] in ("approval_request", "question"), \
                f"재제안이 안 나옴: {names}"
            print(f"✅ ⑦ 4xx 후 실행 생존 → {events[-1][0]} 로 재제안 ({names})", flush=True)
    finally:
        backend_mod.backend.write = original


async def main() -> None:
    try:
        await unit_tests()
        await integration_test()
    finally:
        # done 훅이 기억 카드용 스토어도 열므로 그쪽도 닫아야 프로세스가 끝난다
        from app.common.checkpoint import close_checkpointer
        from app.memory.store import close_memory_store
        await asyncio.sleep(1)          # 발사 후 망각 태스크 잔여분 소화
        await close_checkpointer()
        await close_memory_store()
    print("\n실패 정책 전부 통과", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
