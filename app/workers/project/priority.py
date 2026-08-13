# app/workers/project/priority.py
"""project / priority 워커 — 남은 일 중 무엇을 먼저 할 것인가.

★ gmail 읽기 도구(async_tools=get_gmail_read_tools) — "김대리와의 최근 메일
반영해서 우선순위 분석해줘" 같은 요청에서, 이 워커가 스스로 판단해 필요하면
gmail_search_emails/gmail_get_email 로 관련 메일을 찾아 근거로 쓴다. 반드시
읽기 전용만 — get_gmail_read_tools() 가 gmail_send_email 을 애초에 안 돌려주고,
run_tool_loop() 이 혹시 몰라 한 번 더 막는다(app/common/llm_client.py 참고).
실제 메일 발송은 이 워커의 몫이 아니다 — 엔진A의 mail 에이전트가
analyze_impact 로 이 분석 결과를 받아온 뒤, 승인 게이트가 걸린
gmail_send_email 로 처리한다."""

from typing import Literal

from pydantic import Field

from app.clients.gmail_mcp_client import get_gmail_read_tools
from app.prompts import priority as prompt
from app.schemas.lenient import LenientModel
from app.tools.project_query import PROJECT_TOOLS
from app.workers.base import WorkerSpec


class PriorityItem(LenientModel):
    task_id: str = Field(description="할 일 id. 컨텍스트에 있는 값을 그대로 쓴다.")
    title: str = ""
    tier: Literal["P0", "P1", "P2", "P3"] = "P2"
    score: int = Field(default=50, description="0~100. 클수록 먼저 해야 한다.")
    due_date: str | None = None
    is_overdue: bool = False
    rationale: str = Field(default="", description="이 순위인 이유 1~2문장. 날짜로 설명할 것")


class PriorityResult(LenientModel):
    """★ 2026-08-11 정리.

    `blocks`(할 일 간 의존 관계)를 제거했다 — DB 에 의존 관계 컬럼이 없어서 모델이
    제목만 보고 지어내던 필드였다. 근거 없는 값을 내놓느니 안 내놓는 게 낫다.
    """

    ranked: list[PriorityItem] = Field(
        default_factory=list,
        description="**컨텍스트에 실린** 열린 할 일을 우선순위 순으로. 전사·전체가 아니다",
    )
    scope_note: str = Field(
        default="",
        description="이 순위가 어느 범위인지 한 줄. 예: '이번 주 기준, p1001 프로젝트 할 일'",
    )
    top_focus: str = Field(default="", description="지금 당장 손대야 할 것 한 줄 요약")
    deprioritizable: list[str] = Field(
        default_factory=list,
        description="마감이 멀어 이번 사이클에서 미룰 수 있는 할 일 id. 사업 판단이 아니라 날짜 판단이다",
    )


SPEC = WorkerSpec(
    domain="project",
    dimension="priority",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=PriorityResult,
    tools=tuple(PROJECT_TOOLS),
    async_tools=get_gmail_read_tools,
    context_sections=("project", "milestones", "todos", "members", "meetings"),
)
