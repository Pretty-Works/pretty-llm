"""
오케스트레이터 분류기 — goal 을 읽고 3분기 중 하나를 고른다

v2 는 진입점이 하나(/api/agent/runs)라 URL 이 아무것도 알려주지 않는다.
그래서 여기서 문장을 보고 판별한다. 분류는 LLM 1콜 + 구조화 출력 —
자유 텍스트로 받으면 오타·설명이 섞여 dict 키로 못 쓴다.

세 갈래의 기준은 "무엇을 원하는가"다:
  simple_query : 지금 상태를 알고 싶다        (조회만, 승인 없음)
  engine_a     : 무언가를 실행·기록하고 싶다   (도구 + 쓰기 승인)
  engine_b     : 깊은 분석·비교·재계획을 원한다 (멀티에이전트, 담당자 2·3)
"""

from __future__ import annotations

from typing import Literal

from langchain.chat_models import init_chat_model
from pydantic import BaseModel

from app.config import settings

ROUTES = ("simple_query", "engine_a", "engine_b")
DOMAINS = ("meeting", "vacation", "project", "task", "schedule", "expense", "mail")


class RouteDecision(BaseModel):
    route: Literal["simple_query", "engine_a", "engine_b"]
    # 요청이 건드리는 도메인 전부. 주 도메인을 맨 앞에 — engine_a 에서
    # 1개면 그 도메인 에이전트로 직행, 2개 이상이면 복합 실행기로 간다.
    domains: list[Literal["meeting", "vacation", "project",
                          "task", "schedule", "expense", "mail"]] = []


_RULES = """당신은 그룹웨어 에이전트의 접수처입니다. 사용자 요청을 분류하세요.

## route — 셋 중 하나
simple_query — 현재 상태를 묻는 단순 조회.
  예: "내 연차 며칠 남았어?", "그룹웨어 프로젝트 회의록 목록 보여줘", "이번주 일정 뭐 있어?"

engine_a — 무언가를 만들거나·기록하거나·신청하는 실행 요청. 삭제·수정 안내 요청도 포함.
  예: "회의록 올려줘", "다음주 화요일 연차 신청해줘", "프로젝트 만들고 싶어", "지난주 회의록 삭제해줘"

⚠️ 메일(Gmail) 관련 요청은 조회든 발송이든 항상 engine_a 다 — "최근 메일 5건 보여줘"
   같은 조회 문장도 simple_query 로 보내지 마라. simple_query 에이전트는 그룹웨어
   내부 데이터만 조회하고 Gmail 도구가 아예 없어서, mail 을 그리로 보내면 응답을
   못 만든다. mail 관련 문장은 무조건 engine_a + domains에 "mail" 포함.
   예: "내 최근 메일 확인해줘", "OO한테 회의 일정 메일로 보내줘", "미확인 메일 있어?"

engine_b — 판단이 필요한 깊은 분석·비교·시뮬레이션·재계획. **분석 결과 자체가 최종 답일 때만.**
  예: "일정이 밀렸는데 어떻게 조정할지 시나리오 분석해줘", "이 프로젝트 리스크 분석해줘",
      "김서준이 빠지면 일정에 어떤 영향이 있어?"

⚠️ 분석을 거쳐 실행까지 해달라는 요청은 engine_a 다 — 실행 에이전트가 분석 도구를
직접 부른다. 실행 동사(신청·등록·저장·잡아줘)가 있으면 분석 언급이 있어도 engine_a.
  "위험한지 분석해보고 괜찮으면 연차 신청해줘" → engine_a (주목적 = 신청)
  "연차 내면 위험한지만 분석해줘" → engine_b (주목적 = 분석)

애매하면: 저장/신청이 필요해 보이면 engine_a, 답만 주면 되면 simple_query.

## domains — 요청이 건드리는 도메인 전부 (빠뜨리지 말 것)
meeting(회의록 — **이미 한 회의의 기록**) · vacation(연차·휴가) · project(프로젝트 자체)
task(할일·업무) · schedule(일정·캘린더 — **앞으로의 약속·미팅 잡기**) · expense(경비·비용)
mail(Gmail 메일 검색·조회·발송 — 그룹웨어 내부가 아니라 외부 이메일 계정)

⚠️ 회의 관련은 시제로 가른다:
  "어제 회의 회의록 써줘" → meeting (지난 회의의 기록)
  "다음 주 화요일 팀미팅 잡아줘" → schedule (미래 약속 — 회의록 아님)

- 주 도메인을 맨 앞에 놓으세요.
- 복합 요청은 전부 나열하세요. 예: "연차 내고 그 기간 회의도 확인해줘"
  → domains: ["vacation", "meeting"]
- ⚠️ 다른 작업의 "대상"으로 언급된 것은 별도 도메인이 아닙니다.
  "그룹웨어 프로젝트에 회의록 올려줘" → ["meeting"] (project 아님 — 회의록의 위치일 뿐)
  "프로젝트 새로 만들어줘" → ["project"] (프로젝트 자체가 작업 대상)
  사용자가 실제로 해달라는 일의 도메인만 넣으세요."""

_llm = None


def _get_llm():
    global _llm
    if _llm is None:
        model = init_chat_model(settings.llm_model, model_provider=settings.llm_provider)
        _llm = model.with_structured_output(RouteDecision)
    return _llm


async def classify(goal: str, screen: str = "HOME",
                   history: list[dict] | None = None) -> RouteDecision:
    """goal → RouteDecision(route, domains — 주 도메인이 맨 앞)

    screen 을 같이 주는 이유: 같은 문장도 화면 따라 의미가 다르다
    (PROJECT_CREATE 화면의 "예산은 3천만원" 은 폼 채우기 = engine_a).

    history(최근 대화)가 필요한 이유: 이어지는 발화는 그 자체로는 분류가
    안 된다 — "이름은 AI 검색 개선이야" 는 직전에 에이전트가 프로젝트 이름을
    물었다는 맥락이 있어야 project 로 간다.
    """
    recent = "\n".join(f"{m['role']}: {m['content']}" for m in (history or [])[-4:])
    content = (f"이전 대화:\n{recent}\n\n" if recent else "") + \
              f"현재 화면: {screen}\n요청: {goal}"

    decision: RouteDecision = await _get_llm().ainvoke([
        {"role": "system", "content": _RULES},
        {"role": "user", "content": content},
    ])
    return decision
