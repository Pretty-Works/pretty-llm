"""
복합 요청 실행기 — 여러 도메인에 걸친 요청을 순서대로 처리한다

"다음주 화요일 연차 내고, 그 기간 회의도 확인해줘"
  = vacation 작업 + meeting 작업. 도메인별 에이전트 구조에서는 한 에이전트가
  둘 다 못 하므로, 오케스트레이터가 분해해서 릴레이로 돌린다:

  classify(domains 2개 이상) → decompose(작업 분해, LLM)
    → [vacation 에이전트: 연차 신청 … approval_request 로 멈춤]  ← 스트림 종료
    → (BE resume) → [연차 완료] → [meeting 에이전트: 회의 확인]
    → done (전체 결과 종합)

설계 결정:
  · 작업 k 는 자기만의 스레드(runId#k)를 쓴다 — 서로 다른 그래프가 한 스레드를
    공유하면 상태가 엉킨다.
  · 진행 상황(어디까지 했나)은 체크포인트와 같은 sqlite 에 계획 테이블로 영속 —
    resume 은 runId 만 오므로, 지금 몇 번째 작업이 멈춰 있는지 파일이 기억해야 한다.
  · 앞 작업의 결과는 뒤 작업의 입력에 얹는다 (연차 날짜를 회의 확인이 이어받는 식).
"""

from __future__ import annotations

import json
from typing import AsyncIterator, Literal

from langchain.chat_models import init_chat_model
from pydantic import BaseModel

from app.common import hitl, sse
from app.common.checkpoint import get_checkpointer
from app.config import settings
from app.orchestrator.domains import agent_for_domain
from app.tools.registry import RunContext


# ── 작업 분해 (LLM) ────────────────────────────────────────
class SubTask(BaseModel):
    # classify.DOMAINS 와 항상 같은 집합이어야 한다 — 여기 안 넣으면 "메일 확인하고
    # 할일도 추가해줘" 처럼 mail 이 낀 복합 요청은 decompose() 구조화 출력 검증에서
    # 실패한다(LLM이 domain="mail" 을 고르고 싶어도 스키마가 막음).
    domain: Literal["meeting", "vacation", "project", "task", "schedule", "expense", "mail"]
    task: str          # 그 도메인 에이전트에게 줄 자립적인 한 문장


class Decomposition(BaseModel):
    subtasks: list[SubTask]


_DECOMPOSE_RULES = """복합 요청을 도메인별 작업으로 나누세요.

규칙:
- 각 작업은 그 도메인 담당자가 이것만 읽고 수행할 수 있는 자립적 문장으로.
  원문의 날짜·이름·수치를 빠뜨리지 말고 옮기세요.
- 실행 순서대로 나열하세요. 앞 작업의 결과가 필요한 작업이 뒤로 갑니다.
- ★ **확인·조회 작업을 저장·신청 작업보다 반드시 앞에 놓으세요.** 사용자가 말한
  순서가 반대여도 마찬가지입니다 — 확인 결과를 보고 판단해야 할 일을 이미 저지른
  뒤에 확인하면 의미가 없습니다.
    예) "다음 주 화요일 연차 내고, 그 기간에 회의 있는지도 확인해줘"
        → ① 회의 확인(schedule)  ② 연차 신청(vacation)   ← 이 순서
        (실측 사고: 말한 순서대로 연차를 먼저 내고 나서 회의를 확인했다)
- 도메인이 같은 작업은 하나로 합치세요."""

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
        _llm = model.with_structured_output(Decomposition)
    return _llm


async def decompose(goal: str, domains: list[str]) -> list[dict]:
    result: Decomposition = await _get_llm().ainvoke([
        {"role": "system", "content": _DECOMPOSE_RULES},
        {"role": "user", "content": f"관련 도메인: {', '.join(domains)}\n요청: {goal}"},
    ])
    return [{"domain": s.domain, "task": s.task} for s in result.subtasks]


# ── 계획 영속 (체크포인트와 같은 sqlite 파일) ──────────────
async def _conn():
    return (await get_checkpointer()).conn


async def save_plan(run_id: str, plan: dict) -> None:
    conn = await _conn()
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS run_plans (run_id TEXT PRIMARY KEY, plan TEXT)")
    await conn.execute(
        "INSERT OR REPLACE INTO run_plans (run_id, plan) VALUES (?, ?)",
        (run_id, json.dumps(plan, ensure_ascii=False)))
    await conn.commit()


async def load_plan(run_id: str) -> dict | None:
    conn = await _conn()
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS run_plans (run_id TEXT PRIMARY KEY, plan TEXT)")
    cur = await conn.execute("SELECT plan FROM run_plans WHERE run_id = ?", (run_id,))
    row = await cur.fetchone()
    return json.loads(row[0]) if row else None


def sub_thread(run_id: str, k: int) -> str:
    return f"{run_id}#{k}"


# ── 릴레이 실행 ────────────────────────────────────────────
async def stream_composite(run_id: str, ctx: RunContext, plan: dict,
                           resume_command=None) -> AsyncIterator[str]:
    """plan['current'] 부터 작업을 이어 돌린다.

    작업이 중간에 멈추면(approval/question) 위치를 저장하고 스트림을 닫는다 —
    BE 가 resume 하면 api 층이 이 함수를 resume_command 와 함께 다시 부른다.
    """
    subtasks = plan["subtasks"]
    last_action: dict | None = None

    for k in range(plan["current"], len(subtasks)):
        st = subtasks[k]
        agent = await agent_for_domain(st["domain"])
        sink: dict = {}
        ctx.action = None          # 작업마다 초기화 — 마지막 작업의 action 이 done 에 실림
        ctx.known_facts.clear()    # 이전 서브태스크(다른 도메인)의 조회 결과가
                                    # 이번 서브태스크의 analyze_impact 로 새는 것을 막는다

        if resume_command is not None:
            agent_input = resume_command
            resume_command = None                    # 첫 작업에만 적용
        else:
            task_text = st["task"]
            if plan["answers"]:
                done_so_far = " / ".join(plan["answers"])
                task_text += f"\n\n(참고 — 앞 작업의 결과: {done_so_far})"
            agent_input = {"messages": [{"role": "user", "content": task_text}]}
            if len(subtasks) > 1:
                yield sse.step(f"[{k + 1}/{len(subtasks)}] {st['task']}")

        async for event in hitl._drive(agent, agent_input, sub_thread(run_id, k),
                                       ctx, route="engine_a", domain=st["domain"],
                                       emit_done=False, result_sink=sink):
            yield event

        if not sink.get("completed"):
            # approval_request/question 으로 멈춤 — 위치 기억하고 스트림 종료
            plan["current"] = k
            await save_plan(run_id, plan)
            return

        plan["answers"].append(sink.get("answer", ""))
        if sink.get("action"):
            last_action = sink["action"]

    plan["current"] = len(subtasks)                  # 완료 표시
    await save_plan(run_id, plan)
    final_answer = "\n".join(a for a in plan["answers"] if a)

    # ★ 8/12 변경 — hitl._drive() 와 동일한 이유로 fire() 대신 await 한다.
    #   채팅 목록 제목(BE title 필드)에 쓸 값을 done 바디에 실어야 하기 때문.
    from app.memory.summarize import summarize_run
    title = await summarize_run(run_id, ctx.conversation_id, ctx.goal or "", final_answer)
    done_payload = {"answer": final_answer, "action": last_action}
    if title:
        done_payload["title"] = title
    yield sse.sse_event("done", done_payload)
