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
from app.tools.meeting_tool import (meeting_candidates, meeting_create,
                                    meeting_detail, meeting_draft_fill,
                                    meeting_list)
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
  절대 지어내지 마라. 먼저 user_me 로 오늘을 확인하고, meeting_candidates
  (projectId, fromDate, toDate) 로 후보를 가져와라(fromDate=오늘-14일 정도,
  toDate=오늘). ★ schedule_list 와 meeting_list 를 따로 불러 네가 직접 날짜를
  비교해서 후보를 거르지 마라 — 그 비교를 코드가 이미 정확히 해서 결과를 준다
  (직접 비교하면 하루에 회의가 여러 건일 때 아직 안 쓴 회의까지 같이 빠질 수
  있다). meeting_candidates 결과에 나온 실제 날짜·제목만 ask_user 의 보기로
  제시한다 — 결과에 "다른 프로젝트 회의가 섞여 있을 수 있다"는 경고가 붙어
  있으니, 제목이 이 프로젝트와 명백히 무관하면 참고만 하고 지어내서 걸러내진
  마라. 후보가 하나뿐이면 묻지 말고 그것으로 진행하고, 결과가 비었으면 "최근
  회의록 없는 회의를 못 찾았어요, 날짜와 프로젝트를 알려주시겠어요?"처럼 자유
  입력으로 물어라 — 보기 없이 날짜를 지어내 옵션으로 보여주는 건 절대 금지.
- ★사용자가 **"회의록 등록했어"·"저장했어"** 처럼 저장 완료를 알리면 (초안을
  받아 화면에서 직접 저장한 경우다) 무엇을 저장했는지 되묻지 말고 이어받아라:
  1. meeting_list 로 그 프로젝트 회의록을 조회해 **가장 최근 것**을 고른다.
  2. meeting_detail 로 전문을 읽는다.
  3. 제목·날짜와 후속 조치(followUp) 내용을 사용자에게 정리해 보여준다.
  4. 후속 조치가 있으면 **할 일로 등록할지 한 문장으로 제안**하고 끝낸다.
     등록 자체는 할일 담당 몫이라 여기서 하지 않는다 — 제안만 하고 멈춰라.
  후속 조치가 비어 있으면 지어내지 말고 "후속 조치는 적혀 있지 않네요"라고 알려라."""


_agent = None


async def get_agent():
    global _agent
    if _agent is None:
        _agent = build_domain_agent(
            [user_me, project_search, project_members, schedule_list, meeting_list,
             meeting_candidates, meeting_detail, meeting_create, meeting_draft_fill,
             analyze_impact, recall, doc_search, ask_user, navigate],
            DOMAIN_PROMPT,
            await get_checkpointer(),
            description_prefix="회의록 저장",
        )
    return _agent