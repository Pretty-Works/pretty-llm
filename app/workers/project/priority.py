# app/workers/project/priority.py
"""project / priority 워커 — 남은 일 중 무엇을 먼저 할 것인가."""

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
    blocks: list[str] = Field(
        default_factory=list, description="이 일이 끝나야 진행되는 다른 할 일 id"
    )
    rationale: str = Field(default="", description="이 순위인 이유 1~2문장")


class PriorityResult(LenientModel):
    ranked: list[PriorityItem] = Field(
        default_factory=list, description="열려 있는 할 일 전체를 우선순위 순으로"
    )
    top_focus: str = Field(default="", description="지금 당장 손대야 할 것 한 줄 요약")
    deprioritizable: list[str] = Field(
        default_factory=list,
        description="이번 사이클에서 빼도 되는 할 일 id. 없으면 빈 배열",
    )


SPEC = WorkerSpec(
    domain="project",
    dimension="priority",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=PriorityResult,
    tools=tuple(PROJECT_TOOLS),
    # ★ 2026-08-13 추가 — 메일 내용을 근거로 우선순위를 판단해야 할 때
    #   (예: "이 메일에서 요청한 기한이 급한데 반영됐는지 봐줘") gmail_search_emails
    #   등을 스스로 부를 수 있게 한다. get_gmail_read_tools() 는 gmail_send_email 을
    #   절대 포함하지 않는 읽기 전용 부분집합이라 승인 없이 자율 호출해도 안전하다
    #   (app/clients/gmail_mcp_client.py 참고). gmail-mcp 가 죽어 있으면 빈 목록으로
    #   폴백되므로 이 워커 자체는 그래도 죽지 않는다.
    async_tool_providers=(get_gmail_read_tools,),
    context_sections=("project", "todos", "members"),
)
