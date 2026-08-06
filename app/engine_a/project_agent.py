"""
프로젝트 에이전트 — 생성(FILL_FORM 전용) · 현황(마일스톤·예산) · 마일스톤 토글

합의(8/3): 프로젝트 생성·수정에는 쓰기 도구가 없다 — 대화로 정보를 모아
fill_form 으로 폼만 채우고, 버튼은 사용자가 누른다.
단 마일스톤 완료 토글(milestone.toggleStatus)은 카탈로그에 있는 정식 쓰기
도구라 이 에이전트가 승인 게이트와 함께 수행한다 (오너·PM 한정).
"""

from __future__ import annotations

from app.common.checkpoint import get_checkpointer
from app.engine_a.domain_agents import build_domain_agent
from app.tools.analyze import analyze_impact
from app.tools.ask_user import ask_user
from app.tools.expense_tool import budget_summary
from app.tools.milestone_tool import milestone_list, milestone_toggle_status
from app.tools.navigate import fill_form, navigate
from app.tools.project_tool import project_members, project_search
from app.tools.user_tool import user_me

DOMAIN_PROMPT = """당신은 그룹웨어의 프로젝트 담당 에이전트입니다.

도메인 규칙:
- 프로젝트 생성·수정은 직접 못 한다 — 대화로 정보를 모아 fill_form 으로 폼을
  채워주면 버튼은 사용자가 누른다. 이걸 사용자에게 숨기지 마라.
  · 최소 이름(name)과 기간(startDate·endDate)은 모아야 폼을 채울 가치가 있다.
    한 번에 하나씩 ask_user 로 묻고, "나중에"라고 하면 그 필드는 비워 둬라.
  · user_me 의 canCreateProject 가 없음이면 생성 제안 자체를 하지 마라.
  · 현재 화면이 PROJECT_CREATE 고 입력된 폼 값이 있으면 그 값은 다시 묻지 마라.
- 프로젝트 현황("어떻게 돼가?")은 project_search + milestone_list + budget_summary 를
  조합해 요약하라.
- 마일스톤 완료 처리는 오너·PM 만 가능하다 (project_search 의 isOwner·myRole 확인).
  대상은 milestone_list 로 특정하고, 순서상 다음 차례(isNext)인 것부터 완료한다."""


_agent = None


async def get_agent():
    global _agent
    if _agent is None:
        _agent = build_domain_agent(
            [user_me, project_search, project_members, milestone_list, budget_summary,
             milestone_toggle_status, analyze_impact, fill_form, ask_user, navigate],
            DOMAIN_PROMPT,
            await get_checkpointer(),
            description_prefix="마일스톤 상태 변경 요청입니다.",
        )
    return _agent
