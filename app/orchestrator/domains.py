"""
도메인 → 에이전트 매핑 — engine_a "승인 에이전트 패밀리"의 명부

도메인별 에이전트 구조(8/4 합의): 뼈대(승인 에이전트)는 같고 메뉴판만 다른
에이전트를 도메인 수만큼 찍어낸다. 여기가 그 명부이고, 새 도메인 에이전트를
만들면 dict 에 한 줄 추가하면 라우팅에 편입된다.

아직 전용 에이전트가 없는 도메인(project·task·schedule·expense)은 임시로
meeting_agent 가 받는다 — 수요일(D)에 채워진다.
"""

from __future__ import annotations

from app.engine_a import leave_agent, meeting_agent, project_agent
from app.engine_a.domain_agents import (get_expense_agent, get_mail_agent,
                                        get_schedule_agent, get_task_agent)

_FACTORY = {
    "meeting": meeting_agent.get_agent,
    "vacation": leave_agent.get_agent,
    "project": project_agent.get_agent,     # 생성은 FILL_FORM, 마일스톤 토글은 승인
    "task": get_task_agent,
    "schedule": get_schedule_agent,
    "expense": get_expense_agent,
    "mail": get_mail_agent,                 # gmail_send_email 은 registry의
                                             # MCP_WRITE_TOOLS 덕에 자동으로 승인 게이트
}

DEFAULT_DOMAIN = "meeting"


async def agent_for_domain(domain: str):
    factory = _FACTORY.get(domain, _FACTORY[DEFAULT_DOMAIN])
    return await factory()
