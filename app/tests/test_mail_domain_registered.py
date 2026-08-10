# app/tests/test_mail_domain_registered.py
"""mail 도메인이 라우팅 표(orchestrator/domains.py)와 분류기 스키마
(orchestrator/classify.py) 양쪽에 실제로 등록됐는지 확인한다.

둘 중 하나만 빠지면:
  · classify.DOMAINS/RouteDecision 에만 없음 → LLM이 mail 을 절대 고를 수 없음
    (프롬프트에도 없고 구조화 출력 스키마에도 없음)
  · domains._FACTORY 에만 없음 → classify가 domains=["mail"] 을 골라도
    agent_for_domain("mail")이 DEFAULT_DOMAIN(meeting)으로 조용히 새 버림 —
    사용자는 "메일 보내줘"라고 했는데 회의록 에이전트가 응답하게 된다.
"""

from __future__ import annotations

from app.orchestrator.classify import DOMAINS, RouteDecision
from app.orchestrator.composite import SubTask
from app.orchestrator.domains import DEFAULT_DOMAIN, _FACTORY, agent_for_domain


def test_mail_in_classify_domains_tuple():
    assert "mail" in DOMAINS


def test_route_decision_schema_accepts_mail():
    """pydantic 리터럴에서 mail이 빠지면, LLM이 mail을 골라도 구조화 출력
    검증 자체가 실패해 classify() 가 예외를 던진다."""
    decision = RouteDecision(route="engine_a", domains=["mail"])
    assert decision.domains == ["mail"]


def test_mail_registered_in_domain_factory():
    assert "mail" in _FACTORY
    assert _FACTORY["mail"] is not _FACTORY[DEFAULT_DOMAIN]  # 임시 대체가 아니라 전용 팩토리


def test_composite_subtask_schema_accepts_mail():
    """복합 요청 분해(decompose)의 구조화 출력 스키마도 mail을 허용해야 한다 —
    안 그러면 '메일 확인하고 할일도 추가해줘' 같은 복합 요청에서 LLM이
    domain="mail"을 고르는 순간 검증 실패로 죽는다."""
    st = SubTask(domain="mail", task="최근 메일 5건 확인")
    assert st.domain == "mail"


async def test_agent_for_domain_mail_resolves_without_crashing():
    """gmail-mcp 서버가 없는(혹은 안 켜진) 테스트 환경에서도 — get_gmail_tools()가
    빈 목록으로 안전 폴백하므로 — 에이전트 생성 자체는 죽지 않아야 한다."""
    agent = await agent_for_domain("mail")
    assert agent is not None
