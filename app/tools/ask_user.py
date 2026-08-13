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

# ★ 2026-08-13 변경 — 예전엔 text 를 "질문 한 줄"로 보고 200자로 잘랐다. 그런데
#   FE 확인 결과 options[i].description(= option_details 로 채우던 값)은 FE
#   컴포넌트에 아예 렌더링 필드가 없어 화면에 안 뜬다는 게 확정됐다(버튼 라벨
#   말고는 보여줄 자리가 없음). FE/BE 는 못 건드리는 제약이므로, 사람이 실제로
#   보는 유일한 설명 채널은 이 text 필드뿐이다 — 그래서 재계획처럼 "현재 상황
#   분석 + 안별 설명(무엇이 바뀌는지·기대효과·리스크)"을 통째로 담아야 하는
#   호출도 있어, 200자는 턱없이 부족했다(잘려서 안 뜨는 것처럼 보인 원인 중
#   하나였을 가능성이 크다). 2000자로 올린다 — 여전히 상한을 두는 이유는 이
#   값이 그대로 SSE question 이벤트로 나가므로, 프롬프트가 실수로 통제를 잃고
#   과도하게 긴 텍스트를 생성했을 때의 방어선일 뿐이다.
QUESTION_TEXT_LIMIT = 2000


@tool
def ask_user(label: str, text: str, options: list[str] | None = None,
             option_details: list[str] | None = None,
             multiple: bool = False,
             runtime: ToolRuntime[RunContext] = None) -> str:
    """작업에 꼭 필요한 정보가 없거나 후보가 여러 개일 때 사용자에게 묻는다.

    지어내서 진행하지 말고 이 도구로 물어라. 단, 이미 대화에 있는 정보를
    다시 묻지 마라. 한 번에 하나만 묻는다.

    ★ 보기(options)를 되도록 채워라. 프로젝트·구성원처럼 조회로 목록을 만들 수 있는
      대상이면 먼저 조회 도구를 불러 후보를 만든 뒤 그 이름들을 보기로 넣는다.
      후보가 하나뿐이면 묻지 말고 그것으로 진행하라.

    label:    질문의 짧은 제목, 명사형 (예: "프로젝트 선택", "회의 장소 입력")
    text:     사용자에게 버튼보다 먼저 보이는 메시지. ★ 2026-08-13 변경 — 더는
              "질문 한 줄"이 아니다. 각 보기(option)의 차이를 사람이 버튼을
              누르기 "전에" 알아야 하는 상황(예: 재계획 조정안 3개)이면, 여기에
              "현재 상황/원인을 설명 → 고려할 수 있는 방향을 안별로 요약(무엇을
              바꾸는지·기대효과·리스크) → 선택을 유도하는 마지막 문장"까지
              하나의 글로 담아라(최대 2000자, 문단 여러 개 가능). 짧게 하나만
              물으면 되는 보통 질문은 예전처럼 한두 문장으로 충분하다.
    options:  보기 목록 — 사용자가 읽을 이름표를 넣는다 (예: "그룹웨어 AI 고도화").
              id 숫자를 넣지 마라. 자유 입력만 받을 거면 생략. 라벨은 짧게만
              (예: "인력 재배치 (추천)") — 설명을 라벨에 욱여넣지 마라, 설명은
              위 text 에 이미 다 있다.
    option_details: options 각 항목의 부가 설명(선택, 개수·순서는 options 와 동일).
              ★ 2026-08-13 — FE 에 이 값을 보여줄 자리가 없다는 게 확인됐다(버튼
              아래에 안 뜬다). 값을 채워도 손해는 없지만(추후 FE 지원 대비) 지금은
              화면에 노출된다고 가정하지 마라 — 설명이 꼭 보여야 하는 내용은
              반드시 위 text 에 써라.
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

    opts = options or []
    details = option_details or []

    # questionId 는 넣지 않는다 — BE 가 주입한다 (규격 명시)
    answer = interrupt({
        "kind": "question",              # hitl._drive 가 approval 과 구분하는 표식
        "label": label[:30],
        "text": text[:QUESTION_TEXT_LIMIT],
        "options": [
            {"id": str(i + 1), "label": o,
             "description": (details[i][:200] if i < len(details) and details[i] else None)}
            for i, o in enumerate(opts)
        ],
        "multiple": multiple,
        "allowFreeText": True,           # 보기가 있어도 자유 입력은 항상 허용
    })
    return f"사용자 답변: {answer}"
