# app/tests/test_replan_agent_gmail_wiring.py
"""replan 에이전트(엔진B)에 gmail 도구를 붙인 것이 mail 도메인(엔진A)과 같은
방식(get_gmail_tools() 재사용, mcp_tools.py는 그대로)인지 확인한다.

★ 이 테스트가 지키는 것
  gmail 실제 구현(mcp_servers/gmail_mcp/mcp_tools.py)은 엔진별로 두 벌 만들
  필요가 없다는 게 이전 대화의 결론이었다 — 그 구현은 네트워크 너머의 MCP
  서버라 누가 부르든 하나다. 엔진B(replan_agent)가 gmail을 쓰게 하는 데
  필요했던 건 mcp_tools.py를 건드리는 게 아니라, 엔진A의 mail 도메인이 이미
  하던 get_gmail_tools() 호출을 replan_agent.get_agent()에서 한 번 더
  부르는 것뿐이었다. 이 테스트는 그 재사용이 실제로 같은 함수를 가리키는지,
  그리고 replan 에이전트 생성 자체가 gmail-mcp 가용성과 무관하게 안전한지
  확인한다.
"""

from __future__ import annotations

from app.clients.gmail_mcp_client import get_gmail_tools as canonical_get_gmail_tools
from app.engine_b.replan_agent import DOMAIN_PROMPT, get_agent
from app.engine_b.replan_agent import get_gmail_tools as replan_agent_get_gmail_tools


def test_replan_agent_imports_the_same_get_gmail_tools_as_mail_domain():
    """엔진B 전용 gmail 구현을 새로 만든 게 아니라 mail 도메인과 똑같은 함수를
    재사용한다 — 이름만 같은 별도 구현이 아니라 진짜 같은 객체인지 확인."""
    assert replan_agent_get_gmail_tools is canonical_get_gmail_tools


def test_domain_prompt_covers_mail_after_apply():
    """반영 후 메일 발송 시나리오에 대한 안내가 프롬프트에 실제로 있는지 —
    없으면 LLM이 gmail_send_email이 도구 목록에 있어도 언제 써야 할지 모른다."""
    assert "gmail_send_email" in DOMAIN_PROMPT
    assert "gmail_connection_status" in DOMAIN_PROMPT


async def test_replan_agent_resolves_without_crashing():
    """gmail-mcp 서버가 없는(혹은 안 켜진) 테스트 환경에서도 — get_gmail_tools()가
    빈 목록으로 안전 폴백하므로 — replan 에이전트 생성 자체는 죽지 않아야 한다.
    propose_replan_scenarios/ask_user/replan_apply 는 gmail 없이도 완전히
    동작해야 하는 핵심 도구라, gmail 유무가 replan 자체를 막으면 안 된다."""
    agent = await get_agent()
    assert agent is not None
