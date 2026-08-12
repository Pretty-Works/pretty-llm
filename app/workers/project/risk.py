# app/workers/project/risk.py
"""project / risk 워커 — 목표 달성을 막을 구체적 위험.

★ gmail 읽기 도구(async_tools=get_gmail_read_tools) — priority 워커와 같은
이유(app/workers/project/priority.py 참고)로 붙였다. 지연·블로커·외부 의존
신호는 내부 API 데이터보다 이메일에 먼저 드러나는 경우가 많아서(예: 외주사가
일정 지연을 메일로 먼저 알리는 경우) risk 축에도 자연스러운 근거원이다.
읽기 전용만 — get_gmail_read_tools() 가 gmail_send_email 을 애초에 안 돌려주고,
run_tool_loop() 이 한 번 더 막는다(app/common/llm_client.py 참고)."""

from typing import Literal

from pydantic import Field

from app.clients.gmail_mcp_client import get_gmail_read_tools
from app.prompts import risk as prompt
from app.schemas.lenient import LenientModel
from app.workers.base import WorkerSpec

# cost 를 뺐다 — 예산은 비용 축이 수치까지 낸다. 여기서 다시 판단하면 결론이 둘이 된다.
RiskCategory = Literal["schedule", "resource", "scope", "quality", "external"]


class RiskItem(LenientModel):
    category: RiskCategory = "schedule"
    title: str = Field(default="", description="위험을 한 줄로")
    subjects: list[str] = Field(
        default_factory=list,
        description="지목 대상 식별자. ['todo:101', 'todo:106'] 처럼 한 칸에 하나씩",
    )
    likelihood: int = Field(default=50, description="발생 가능성 0~100")
    impact: int = Field(default=50, description="발생 시 타격 0~100")
    risk_score: int = Field(default=25, description="round(likelihood * impact / 100)")
    evidence_note: str = Field(
        default="", description="어떤 데이터를 보고 이렇게 판단했는지"
    )
    mitigation: str = Field(default="", description="실행 가능한 완화책")


class RiskResult(LenientModel):
    overall_risk_score: int = Field(
        default=0, description="0~100. 개별 위험의 평균이 아니라 종합 판단"
    )
    risks: list[RiskItem] = Field(default_factory=list)
    early_warnings: list[str] = Field(
        default_factory=list,
        description="아직 위험은 아니지만 지켜봐야 할 신호",
    )


SPEC = WorkerSpec(
    domain="project",
    dimension="risk",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=RiskResult,
    # 내부 조회 도구를 뺐다 — 이 축은 조사하지 않는다. 컨텍스트에 이미 계산되어
    # 들어온 사실만 조합한다. 도구를 주면 다른 축이 보는 것을 또 보게 된다.
    tools=(),
    async_tools=get_gmail_read_tools,
    context_sections=("project", "milestones", "todos", "members", "meetings", "budget", "leaves", "workload"),
)
