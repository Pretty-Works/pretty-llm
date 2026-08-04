"""
프로젝트 에이전트 — 생성 대화 전담 (FILL_FORM 전용)

합의(8/3): 프로젝트 생성은 POST 도구가 없다 — 에이전트에게 생성 권한 자체가
없고, 대화로 정보를 모아 폼을 채워주면(FILL_FORM) 생성 버튼은 사용자가 누른다.
그래서 이 에이전트에는 쓰기 도구가 하나도 없고, 승인 미들웨어도 없다.
(fill_form 은 DB 를 건드리지 않으므로 승인 대상이 아니다)

대화 흐름 (A-2):
  "프로젝트 만들고 싶어" → ask_user 로 필수 정보 수집(이름·기간 등)
  → 충분해지면 fill_form 호출 → done + action=FILL_FORM
  → 사용자가 PROJECT_CREATE 화면에 있으면(goal 에 화면 정보가 실려 옴)
    이미 입력된 값은 다시 묻지 않고 나머지만 채운다 (실시간 채움)
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from app.common.checkpoint import get_checkpointer
from app.config import settings
from app.tools.ask_user import ask_user
from app.tools.navigate import fill_form
from app.tools.project_tool import project_members, project_search
from app.tools.registry import RunContext

SYSTEM_PROMPT = """당신은 그룹웨어의 프로젝트 생성 도우미입니다.

당신은 프로젝트를 직접 만들 수 없습니다 — 대화로 정보를 모아 fill_form 으로
생성 폼을 채워주면, 생성 버튼은 사용자가 직접 누릅니다. 이걸 사용자에게도
숨기지 마세요.

절대 규칙 — 물어볼 게 있으면 반드시 ask_user 도구로 물으세요. 텍스트 답변은
작업 완료 보고에만 씁니다. 텍스트로 질문하면 사용자 화면에 입력창이 뜨지 않아
대화가 끊깁니다.
  잘못:  (답변으로) "프로젝트 이름을 알려주실 수 있나요?"
  올바름: ask_user(label="프로젝트 이름", text="새 프로젝트의 이름을 알려주세요.")

진행 방법:
1. 최소한 이름(name)과 기간(startDate·endDate)은 있어야 폼을 채울 가치가
   있습니다. 없으면 ask_user 로 모으세요. 한 번에 하나씩.
2. 예산·설명·멤버·마일스톤은 사용자가 말했으면 넣고, 안 했으면 한 번만 묻고
   "나중에"라고 하면 비워 두세요. 지어내지 마세요.
3. 정보가 모이면 fill_form 을 호출하고, 무엇을 채웠고 무엇이 비었는지
   한두 문장으로 보고하세요.
4. 현재 화면이 PROJECT_CREATE 고 이미 입력된 폼 값이 있으면, 그 값은 다시
   묻지 말고 비어 있는 필드만 다루세요."""


def build_project_agent(checkpointer):
    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
    return create_agent(
        model,
        tools=[project_search, project_members, ask_user, fill_form],
        system_prompt=SYSTEM_PROMPT,
        context_schema=RunContext,
        checkpointer=checkpointer,      # ask_user(question) 재개에 필요
    )


_agent = None


async def get_agent():
    global _agent
    if _agent is None:
        _agent = build_project_agent(await get_checkpointer())
    return _agent
