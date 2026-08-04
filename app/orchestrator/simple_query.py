"""
simple_query 핸들러 — 조회 전용 에이전트

engine_a 와 같은 create_agent 지만 세 가지가 없다:
  · 쓰기 도구 없음        → 승인 게이트 자체가 불필요 (미들웨어 없음)
  · ask_user 없음         → interrupt 가 절대 없다 = 한 세그먼트에 반드시 done
  · checkpointer 없음     → 재개할 일이 없으니 상태를 남기지 않는다

"조회는 게이트 없이 반환"(팀 다이어그램)이 이 구성으로 구현된다.
정보가 애매하면 done.answer 로 물어보면 된다 — 다음 발화는 새 Run 으로 오고,
직전 대화가 messages(최근 10건)에 실려 오므로 맥락이 이어진다.
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from app.config import settings
from app.tools.meeting_tool import meeting_list
from app.tools.project_tool import project_members, project_search
from app.tools.registry import RunContext

SYSTEM_PROMPT = """당신은 그룹웨어의 조회 담당 에이전트입니다. 묻는 것에만 간결히 답합니다.

원칙:
- 프로젝트를 이름으로 말하면 project_search 로 ID 를 찾은 뒤 조회하세요.
- 조회 결과에 없는 내용을 지어내지 마세요. 없으면 없다고 답하세요.
- 답은 2~3문장 이내. 목록은 항목당 한 줄로.
- 무엇을 조회할지 애매하면 짧게 되물어보세요 (이 답변 자체가 질문이어도 됩니다)."""


def build_simple_agent():
    model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
    return create_agent(
        model,
        tools=[project_search, project_members, meeting_list],   # 조회만
        system_prompt=SYSTEM_PROMPT,
        context_schema=RunContext,
        # 미들웨어·checkpointer 없음 — 위 모듈 주석 참고
    )


_agent = None


async def get_agent():
    """다른 핸들러와 시그니처를 맞추기 위한 async (내부는 동기 생성)."""
    global _agent
    if _agent is None:
        _agent = build_simple_agent()
    return _agent
