"""
ask_user — 에이전트가 사용자에게 되묻는 도구 (question 이벤트의 근원)

approval_request 가 "미들웨어가 쓰기 도구를 가로채서" 멈추는 것이라면,
question 은 "에이전트가 스스로 interrupt() 를 불러서" 멈춘다 (교재
03_LangGraph HITL 의 human_assistance 패턴). 멈춘 뒤의 동작은 둘이 같다:
체크포인트 저장 → 스트림 종료 → BE 가 답을 모아 /resume → 재개.

interrupt() 가 던진 payload 는 hitl._drive 가 받아 question 이벤트로
변환하고, resume 때 Command(resume=답변) 의 값이 이 함수의 interrupt()
반환값으로 돌아온다 — 즉 LLM 눈에는 "부르면 사용자 답이 돌아오는 도구"다.

규격 제한: question 은 Run 당 5회 (초과는 AGENT_022). 한도에 걸리면
interrupt 없이 거절 문구를 반환해 LLM 이 아는 정보로 마무리하게 한다.
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool
from langgraph.types import interrupt

from app.tools.registry import RunContext

QUESTION_LIMIT = 5          # 규격: Run 당 5회 초과 시 AGENT_022 (BE 도 자체 차단)


@tool
def ask_user(label: str, text: str, options: list[str] | None = None,
             multiple: bool = False,
             runtime: ToolRuntime[RunContext] = None) -> str:
    """작업에 꼭 필요한 정보가 없거나 후보가 여러 개일 때 사용자에게 묻는다.

    지어내서 진행하지 말고 이 도구로 물어라. 단, 이미 대화에 있는 정보를
    다시 묻지 마라.

    ★ **한 턴에 이 도구를 두 번 이상 부르지 마라.** 물어볼 게 여러 개여도 이번엔
      하나만 묻고, 답을 받은 뒤 다음 것을 물어라 — 두 개를 동시에 부르면 사용자
      화면에는 어차피 하나만 뜨고, 나머지 하나가 답을 못 받은 채 남아 재개가
      실패한다(실측: 실행이 통째로 에러로 끝났다).

    ★ 보기(options)를 되도록 채워라. 프로젝트·구성원처럼 조회로 목록을 만들 수 있는
      대상이면 먼저 조회 도구를 불러 후보를 만든 뒤 그 이름들을 보기로 넣는다.
      후보가 하나뿐이면 묻지 말고 그것으로 진행하라.

    label:    질문의 짧은 제목, 명사형 (예: "프로젝트 선택", "회의 장소 입력")
    text:     사용자에게 보여줄 질문 문장
    options:  보기 목록 — 사용자가 읽을 이름표를 넣는다 (예: "그룹웨어 AI 고도화").
              id 숫자를 넣지 마라. 자유 입력만 받을 거면 생략
    multiple: 보기를 여러 개 고를 수 있는가 (참석자 고르기 등)
    """
    # 5회 제한 — 지금까지 이 도구가 답한 횟수를 대화 기록에서 센다.
    #   (interrupt 전에 세야 한다. BE 도 자체 카운트로 AGENT_022 를 낸다)
    messages = (runtime.state or {}).get("messages", []) if runtime else []
    asked = sum(1 for m in messages
                if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "ask_user")
    if asked >= QUESTION_LIMIT:
        return (f"질문 한도({QUESTION_LIMIT}회)를 넘어 더 물을 수 없습니다. "
                "지금까지 아는 정보로 진행하거나, 부족하면 작업을 정리하고 종료하세요.")

    # questionId 는 넣지 않는다 — BE 가 주입한다 (규격 명시)
    answer = interrupt({
        "kind": "question",              # hitl._drive 가 approval 과 구분하는 표식
        "label": label[:30],
        "text": text[:200],
        "options": [{"id": str(i + 1), "label": o, "description": None}
                    for i, o in enumerate(options or [])],
        "multiple": multiple,
        "allowFreeText": True,           # 보기가 있어도 자유 입력은 항상 허용
    })
    return f"사용자 답변: {answer}"
