# app/tests/test_mail_agent_analyze_impact.py
"""메일 도메인이 task/schedule/expense 와 같은 패턴으로 analyze_impact 를 쓰는지 확인한다.

★ 배경
  "이번 프로젝트 우선순위 분석해서 팀원들한테 메일로 보내줘" 같은 요청은 메일
  에이전트 혼자서는 못 푼다 — 실제 심층 분석(우선순위 계산)은 엔진B 워커들의
  일이고, 메일 에이전트는 그 결과를 받아 메일로 포장/발송하는 역할만 해야 한다.
  task/schedule/expense 에이전트가 이미 analyze_impact 를 도구로 갖고 이 방식
  (엔진A가 필요하면 analyze_impact 로 엔진B 를 호출)을 쓰고 있었는데, 메일
  에이전트만 빠져 있었다 — 그래서 LLM이 분석 결과를 지어내 메일에 넣을 위험이
  있었다. 이 테스트는 메일 에이전트도 같은 패턴을 따르는지 확인한다.
"""

from __future__ import annotations

from app.tools.analyze import analyze_impact


def test_mail_prompt_instructs_analyze_impact_before_composing():
    """분석이 필요한 메일 요청에서 지어내지 말고 analyze_impact 를 쓰라는 안내가
    프롬프트에 실제로 있는지 — 없으면 LLM이 분석 결과를 지어낼 수 있다."""
    from app.engine_a.domain_agents import MAIL_PROMPT

    assert "analyze_impact" in MAIL_PROMPT


async def test_get_mail_agent_builds_with_analyze_impact_tool(monkeypatch):
    """get_mail_agent() 가 build_domain_agent() 에 넘기는 tools 목록에
    analyze_impact 가 실제로 포함되는지 — 프롬프트 안내만 있고 도구가 없으면
    LLM은 그 도구를 호출조차 못 한다."""
    import app.engine_a.domain_agents as domain_agents

    domain_agents._agents.pop("mail", None)  # 이전 테스트가 캐시해 뒀을 수 있다

    captured: dict = {}

    def fake_build_domain_agent(tools, prompt, checkpointer, description_prefix=None):
        captured["tools"] = tools
        return "fake-mail-agent"

    async def fake_get_checkpointer():
        return object()

    monkeypatch.setattr(domain_agents, "build_domain_agent", fake_build_domain_agent)
    monkeypatch.setattr(domain_agents, "get_checkpointer", fake_get_checkpointer)

    await domain_agents.get_mail_agent()

    assert analyze_impact in captured["tools"]
