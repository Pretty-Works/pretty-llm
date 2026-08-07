# app/common/run_context.py
"""실행 중인 Run 의 X-Run-Id 전파 — 도구 시그니처를 바꾸지 않고 contextvar 로 나른다.

engine_b/runner.py 가 실행 시작 시 set 하고, LLM 이 자율 호출하는 조회 도구들이
clients/backend.py 를 부를 때 읽는다. asyncio Task 생성 시 컨텍스트가 복사되므로
병렬 워커에도 그대로 전파된다. 미설정(테스트·직접 호출)이면 None — 도구는 픽스처로 폴백.
"""

from contextvars import ContextVar

current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)
