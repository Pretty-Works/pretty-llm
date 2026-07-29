"""
Scenario Executor — Replanning 3개 시나리오 병렬 생성

흐름:
1. 프로젝트 태스크/인력 조회 (Tool)
2. 고정 3개 시나리오를 asyncio.gather로 병렬 실행
   - 일정연장: 인력 유지, 마감일 조정
   - 인력추가: 마감일 유지, 투입 인력 추가
   - 범위축소: 인력/마감 유지, 태스크 제거·연기
3. ScenarioExecutorOutput 반환 → tradeoff.py로 전달
"""

import asyncio
import json

import httpx
from langchain_openai import ChatOpenAI

from app.config import settings
from app.schemas.state import (
    AgentOutput,
    ReplanningInput,
    ScenarioExecutorOutput,
    ScenarioResult,
)

llm = ChatOpenAI(model=settings.llm_model, api_key=settings.llm_api_key)

# ── 시나리오 타입 ──────────────────────────────────────────────
SCENARIO_EXTEND = "일정연장"
SCENARIO_ADD_RESOURCE = "인력추가"
SCENARIO_REDUCE_SCOPE = "범위축소"

# ── Tool ──────────────────────────────────────────────────────

async def fetch_project_tasks(http: httpx.AsyncClient, project_id: int) -> list[dict]:
    response = await http.get(f"/api/v1/projects/{project_id}/tasks")
    response.raise_for_status()
    return response.json().get("result", [])


async def fetch_project_members(http: httpx.AsyncClient, project_id: int) -> list[dict]:
    response = await http.get(f"/api/v1/projects/{project_id}/members")
    response.raise_for_status()
    return response.json().get("result", [])


# ── 시나리오별 LLM 호출 ───────────────────────────────────────

SCENARIO_SYSTEM = """
당신은 프로젝트 리플래닝 전문가입니다.
주어진 시나리오 유형에 맞게 구체적인 변경 계획을 수립하세요.

반드시 JSON으로만 응답하세요.
형식:
{{
  "changes": ["변경사항1", "변경사항2", ...],
  "expected_outcome": "예상 결과",
  "risks": ["리스크1", "리스크2", ...],
  "reasoning": "판단 근거",
  "confidence": 0.85
}}
"""

SCENARIO_USER = """
[문제 상황]
{problem}

[시나리오 유형]
{scenario_type}

[현재 태스크 현황]
{tasks}

[팀원 현황]
{members}

위 시나리오 유형에 맞는 구체적인 리플래닝 방안을 제시해주세요.
"""


def _format_tasks(tasks: list[dict]) -> str:
    if not tasks:
        return "없음"
    return "\n".join(
        f"- [{t.get('status', '')}] {t.get('title', '')} / 마감: {t.get('dueDate', '없음')}"
        for t in tasks
    )


def _format_members(members: list[dict]) -> str:
    if not members:
        return "없음"
    return "\n".join(
        f"- {m.get('name', '')} ({m.get('role', '')})"
        for m in members
    )


async def _run_scenario(
    scenario_type: str,
    problem: str,
    tasks: list[dict],
    members: list[dict],
) -> ScenarioResult:
    user_msg = SCENARIO_USER.format(
        problem=problem,
        scenario_type=scenario_type,
        tasks=_format_tasks(tasks),
        members=_format_members(members),
    )

    # 최대 2회 재시도
    for attempt in range(2):
        try:
            response = await llm.ainvoke([
                {"role": "system", "content": SCENARIO_SYSTEM},
                {"role": "user", "content": user_msg},
            ])
            raw = json.loads(response.content)

            if raw.get("changes") and raw.get("expected_outcome"):
                return ScenarioResult(
                    scenario_type=scenario_type,
                    changes=raw.get("changes", []),
                    expected_outcome=raw.get("expected_outcome", ""),
                    risks=raw.get("risks", []),
                    agent_outputs=[
                        AgentOutput(
                            dimension=scenario_type,
                            result=raw,
                            reasoning=raw.get("reasoning", ""),
                            confidence=float(raw.get("confidence", 0.7)),
                        )
                    ],
                )
        except Exception:
            if attempt == 1:
                raise

    # 재시도 모두 실패 시 빈 결과 반환
    return ScenarioResult(
        scenario_type=scenario_type,
        changes=[],
        expected_outcome="생성 실패",
        risks=[],
        agent_outputs=[],
    )


# ── 메인 실행 ─────────────────────────────────────────────────

async def run(input_data: ReplanningInput, http: httpx.AsyncClient) -> ScenarioExecutorOutput:
    # Tool 호출
    tasks, members = await asyncio.gather(
        fetch_project_tasks(http, input_data.project_id),
        fetch_project_members(http, input_data.project_id),
    )

    # 3개 시나리오 병렬 실행
    scenario_extend, scenario_add, scenario_reduce = await asyncio.gather(
        _run_scenario(SCENARIO_EXTEND, input_data.problem, tasks, members),
        _run_scenario(SCENARIO_ADD_RESOURCE, input_data.problem, tasks, members),
        _run_scenario(SCENARIO_REDUCE_SCOPE, input_data.problem, tasks, members),
    )

    return ScenarioExecutorOutput(
        project_id=input_data.project_id,
        problem=input_data.problem,
        scenarios=[scenario_extend, scenario_add, scenario_reduce],
    )
