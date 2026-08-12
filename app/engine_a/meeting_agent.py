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
from app.tools.meeting_tool import (meeting_create, meeting_detail,
                                    meeting_draft_fill, meeting_list)
from app.tools.memory_tool import doc_search, recall
from app.tools.navigate import navigate
from app.tools.project_tool import project_members, project_search
from app.tools.schedule_tool import schedule_list
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
- 삭제·수정 요청은 meeting_list 로 대상을 특정한 뒤 navigate(MEETING_DETAIL) 로 안내.
- ★파일이 첨부된 회의록 작성 요청은 meeting_create 로 저장하지 말고
  meeting_draft_fill 로 작성 화면에 초안만 채워라 — 저장은 사용자가 화면에서 한다.
  첨부 없이 말로 불러 주는 내용은 기존대로 meeting_create 로 저장한다.
- ★ "회의록 작성해줘"처럼 날짜(그리고 프로젝트)를 안 짚어 주면, 어느 날짜인지
  절대 지어내지 마라. 먼저 user_me 로 오늘을 확인하고, schedule_list 로 최근
  1~2주 지난 일정 중 type=MEETING 인 것을 조회해 후보를 만들어라(같은 기간의
  회의록이 meeting_list 에 이미 있으면 그건 후보에서 뺀다 — 이미 작성된 것).
  그 조회로 찾은 실제 날짜·제목만 ask_user 의 보기로 제시한다. 후보가 하나뿐이면
  묻지 말고 그것으로 진행하고, 아예 없으면 "최근 회의 일정을 못 찾았어요, 날짜와
  프로젝트를 알려주시겠어요?"처럼 자유 입력으로 물어라 — 보기 없이 날짜를 지어내
  옵션으로 보여주는 건 절대 금지."""


_agent = None


async def get_agent():
    global _agent
    if _agent is None:
        _agent = build_domain_agent(
            [user_me, project_search, project_members, schedule_list, meeting_list,
             meeting_detail, meeting_create, meeting_draft_fill, analyze_impact,
             recall, doc_search, ask_user, navigate],
            DOMAIN_PROMPT,
            await get_checkpointer(),
            description_prefix="회의록 저장",
        )
    return _agent