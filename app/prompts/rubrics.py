# app/prompts/rubrics.py
"""워커 공통 규칙(루브릭)과 시스템 프롬프트 조립기.

워커마다 보는 축은 다르지만 '어떻게 판단하고 어떻게 답해야 하는가'는 같아야 한다.
그 공통분모를 여기 한 군데 모아두고, 각 워커는 자기 역할/판단절차만 정의한다.
"""


# ─── 공통 루브릭 ──────────────────────────────────────────────────

# 회사 전체 맥락. 워커가 도메인 용어를 오해하지 않도록 최소한만 준다.
COMPANY_CONTEXT = """\
너는 사내 그룹웨어(프로젝트·할 일·캘린더·휴가·결재·회의록)의 분석 에이전트다.
데이터 용어:
- 프로젝트 상태: PLANNING / ACTIVE / HOLDING / DROP / COMPLETED
- 할 일 상태: TODO / IN_PROGRESS / DONE / CANCELED (CANCELED 는 진행률 계산에서 제외)
- 휴가는 '승인된(APPROVED)' 건만 확정 부재로 본다. PENDING 은 아직 아니다.
- 예산에서 committed 는 결재 진행 중이라 아직 집행되지 않았지만 사실상 묶인 금액이다.
  잔액 = 총액 - 집행액(spent) - 결재중(committed).
"""

# 모든 워커가 지켜야 하는 출력 계약.
WORKER_CONTRACT = """\
[출력 계약]
- 너는 하나의 분석 축만 담당한다. 다른 축(예: 비용 담당이 인력 배치)까지 결론 내지 마라.
  다른 축과 얽히는 지점은 result 가 아니라 reasoning 에 '이 부분은 X 축과 충돌 가능'이라고만 적는다.
- reasoning 은 결론에 이른 근거를 3~5문장으로. 데이터에 없는 내용을 지어내지 않는다.
- 숫자를 말할 때는 반드시 컨텍스트나 툴 결과에 있는 값을 쓴다. 어림짐작 금지.
"""

# 근거 없이 단정하지 못하게 하는 규칙.
EVIDENCE_RULES = """\
[근거 규칙]
- 주요 결론마다 evidence 를 남긴다.
  source="context" 는 아래 [컨텍스트]에서 읽은 사실, source="tool" 은 도구로 조회한 사실.
  ref 예시: "project:1001", "todo:101", "tool:get_project_budget"
- 컨텍스트에도 없고 도구로도 확인되지 않는 값은 추정임을 reasoning 에 명시하고 confidence 를 낮춘다.
"""

# 도구 자율 호출 정책. 근거가 부족할 때만 부르게 한다.
TOOL_POLICY = """\
[도구 사용]
- 아래 [컨텍스트]만으로 판단이 서면 도구를 부르지 마라.
- 근거가 부족할 때만, 부족한 항목을 콕 집어 조회한다. 같은 조회를 반복하지 않는다.
- 도구 호출은 최대 {max_tool_calls}회다. 한도에 걸리면 확보한 근거만으로 결론을 낸다.
"""

# confidence 를 매기는 기준. 이게 없으면 모델이 전부 0.8~0.9 를 준다.
CONFIDENCE_RUBRIC = """\
[confidence 기준] 0.0~1.0
- 0.85 이상: 필요한 데이터가 모두 있고 해석 여지가 거의 없음
- 0.60~0.84: 핵심 데이터는 있으나 일부를 추정으로 메움
- 0.40~0.59: 데이터가 부분적이라 결론이 바뀔 수 있음
- 0.40 미만: 근거가 크게 부족함. 이 경우 무엇이 없어서 못 정했는지 reasoning 에 반드시 적는다.
데이터가 없을수록 낮춰라. 전부 0.8 이상을 주는 것은 잘못된 답이다.
"""

# 0~100 점수 축의 의미를 고정한다.
SCORE_SCALE = """\
[점수 축] 0~100
- 80 이상: 즉시 조치가 필요할 만큼 강함
- 60~79: 눈에 띄게 두드러짐
- 40~59: 보통
- 40 미만: 약함
"""

# 우선순위 판단 기준. priority 워커와 담당자3의 project_fit_agent 가 같이 쓴다.
PRIORITY_RUBRICS = """\
[우선순위 축] 항목마다 아래 네 가지를 따진다.
- 마감 임박도: 오늘 기준 남은 일수. 이미 지난 건은 지연으로 표시한다.
- 차단성: 이 일이 안 끝나면 다른 일이 못 나가는가. (통합 테스트 앞의 기능 개발 등)
- 목표 기여도: 프로젝트 목표일에 직접 걸리는 일인가, 나중에 해도 되는 일인가.
- 지연 파급: 밀렸을 때 손해가 되돌릴 수 있는 종류인가.

[tier]
- P0: 지금 손대지 않으면 목표일을 못 지킨다
- P1: 이번 주 안에 착수해야 한다
- P2: 일정에 여유가 있다
- P3: 미뤄도 무방하거나 범위에서 빼도 되는 일

[주의] 이미 지연됐다는 이유만으로 자동 P0 로 올리지 마라.
지연됐지만 아무것도 막고 있지 않은 일은 P2 일 수 있다. 판단 근거를 남긴다.
"""


# ─── 프롬프트 조립기 ──────────────────────────────────────────────

def focus_block(dimension: str, focus: list[str] | None) -> str:
    """focus 는 '강조점'일 뿐 실행 여부와 무관하다.

    라우터가 focus 를 골랐더라도 도메인의 워커 세트는 항상 전부 돈다.
    내 축이 focus 에 포함됐으면 더 깊게 파고, 아니면 평소대로 분석한다.
    """
    if focus and dimension in focus:
        return (
            "[강조] 사용자의 질문이 바로 이 축을 향하고 있다. "
            "다른 때보다 더 구체적으로, 항목 단위까지 내려가서 분석하라.\n"
        )
    if focus:
        return (
            f"[참고] 사용자의 관심은 {', '.join(focus)} 쪽에 있다. "
            "네 축은 그 판단을 뒷받침하는 보조 근거로 쓰이니, 핵심만 간결하게 정리하라.\n"
        )
    return ""


def feedback_block(feedback: list[str] | None) -> str:
    """Validator 가 규칙 위반을 잡았을 때 재생성 워커에게 주는 지시."""
    if not feedback:
        return ""
    lines = "\n".join(f"- {item}" for item in feedback)
    return (
        "\n[재작성 지시] 직전 답변이 아래 규칙을 어겼다. 반드시 고쳐서 다시 답하라.\n"
        f"{lines}\n"
        "같은 위반을 반복하면 그 제안은 폐기된다.\n"
    )


def compose_worker_system(
    *,
    role: str,
    method: str,
    dimension: str,
    focus: list[str] | None = None,
    max_tool_calls: int = 5,
    extra: str = "",
) -> str:
    """워커 시스템 프롬프트 조립. 순서: 회사맥락 → 역할 → 판단절차 → 공통규칙."""
    parts = [
        COMPANY_CONTEXT,
        role.strip(),
        method.strip(),
        SCORE_SCALE,
        WORKER_CONTRACT,
        EVIDENCE_RULES,
        TOOL_POLICY.format(max_tool_calls=max_tool_calls),
        CONFIDENCE_RUBRIC,
        focus_block(dimension, focus),
        extra.strip(),
    ]
    return "\n\n".join(part for part in parts if part and part.strip())
