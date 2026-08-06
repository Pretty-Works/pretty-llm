"""
회의록 에이전트 — engine_a 도메인 에이전트 (승인 에이전트 패밀리)

뼈대는 domain_agents.build_domain_agent 공용 — 여기는 메뉴판과 도메인 규칙만.
공통 규칙(상대 날짜=user_me 먼저, ask_user 되묻기, 삭제=navigate 등)은
prompt_rules.COMMON_RULES 로 전 에이전트가 공유한다.
"""

from __future__ import annotations

from app.common.checkpoint import get_checkpointer
from app.engine_a.domain_agents import build_domain_agent
from app.tools.analyze import analyze_impact
from app.tools.ask_user import ask_user
from app.tools.meeting_tool import meeting_create, meeting_detail, meeting_list
from app.tools.navigate import navigate
from app.tools.project_tool import project_members, project_search
from app.tools.user_tool import user_me

DOMAIN_PROMPT = """당신은 그룹웨어의 회의록 담당 에이전트입니다.

도메인 규칙:
- 프로젝트는 이름이 아니라 ID 로 다룬다. 모르면 project_search 로 찾고,
  후보가 여럿이면 그 목록을 ask_user 의 보기로 줘라.
- 참석자는 userId 목록이다 — project_members 로 변환하되, ★본인(isMe)은
  참석자에 넣지 마라 (작성자는 자동 포함, 넣으면 거부됨).
- 회의록은 오늘이거나 과거의 회의만 기록할 수 있다. 미래 회의 요청이면
  저장하지 말고 "일정으로 잡아드릴까요?" 라고 안내하라.
- 회의 날짜는 프로젝트 기간 안이어야 한다 (project_search 결과의 기간 참고).
- 지난 회의 내용 질문은 meeting_list 로 특정 → meeting_detail 로 전문 조회.
- 삭제·수정 요청은 meeting_list 로 대상을 특정한 뒤 navigate(MEETING_DETAIL) 로 안내."""


_agent = None


async def get_agent():
    global _agent
    if _agent is None:
        _agent = build_domain_agent(
            [user_me, project_search, project_members, meeting_list, meeting_detail,
             meeting_create, analyze_impact, ask_user, navigate],
            DOMAIN_PROMPT,
            await get_checkpointer(),
            description_prefix="회의록 저장 요청입니다.",
        )
    return _agent
