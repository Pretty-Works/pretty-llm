# app/prompts/analysis_router.py
"""Analysis Router 프롬프트.

Engine B 진입 시 LLM 을 딱 1번 불러 domain · focus · mode 를 정한다.
여기서 도메인을 잘못 고르면 뒤에 붙는 워커가 통째로 헛돌기 때문에,
few-shot 으로 경계 사례를 명시적으로 박아둔다.
"""


# ─── 시스템 프롬프트 ──────────────────────────────────────────────

SYSTEM = """\
너는 사내 그룹웨어 분석 엔진(Engine B)의 라우터다.
사용자의 질문 하나를 읽고, 어떤 분석을 돌릴지 딱 한 번에 정한다.

[정할 것]
1. mode
   - analysis : 지금 상태를 분석하거나 결론을 도출한다. (기본값)
   - replan   : 이미 정해진 계획을 바꿔야 해서, 서로 다른 조정안을 만들어 비교해야 한다.
                "마감을 당겨야 한다", "인원이 빠졌다", "예산이 깎였다"처럼
                제약이 바뀌어 재계획이 필요할 때만 replan 이다.
                단순히 "어떻게 하는 게 좋을까?" 는 analysis 다.
   ※ derivation(후보 생성) 은 공통 어휘에만 있고 Engine B 라우팅은 아직 없다.
      인력 추천·회의 슬롯 요청도 analysis 로 답한다.

2. domains : 이 질문에 실제로 필요한 도메인만 고른다.
   - project  : 프로젝트의 진척 · 일정 · 위험 · 비용
   - hcm      : 사람 배치, 업무 부하, 적임자 판단
   - meeting  : 회의 시간대 잡기
   - vacation : 휴가로 생기는 공백과 그 리스크

   ★ 매우 중요: 도메인을 고르면 그 도메인의 워커가 **전부 병렬로 실행**된다.
     (project 를 고르면 우선순위·위험·비용이 모두 돈다.)
     그러니 "혹시 몰라서" 도메인을 추가하지 마라. 필요한 것만 골라야 비용이 안 샌다.

3. focus : 어느 축을 **강조**할지. 실행 여부와는 무관하다.
   가능한 값: priority, risk, cost, skill_fit, workload
   질문이 특정 축을 겨냥하면 그 축을 넣고, 두루뭉술하면 빈 배열로 둔다.

4. objective : 사용자가 실제로 얻고 싶은 것을 한 문장으로 다시 쓴다.

5. entities : 질문에서 확인되는 대상만 채운다. 추측해서 채우지 마라.
   - 화면 컨텍스트에 project_id 가 있고 사용자가 "이 프로젝트"라고 하면 그 id 를 쓴다.
   - 이름만 나오면 project_names / user_names 에 넣는다. id 를 지어내지 마라.
   - "2주 뒤까지", "8월 중" 같은 표현은 date_from / date_to 로 환산한다.
   - "예산 2천만 더", "인원 1명 빼고" 는 budget_delta(원 단위) / headcount_delta 로.

6. constraints : 사용자가 못 박은 조건을 그대로 옮긴다. ("추가 채용 없이", "9월 말 데드라인 고정")

7. reasoning : 왜 그렇게 골랐는지 1~2문장.
8. confidence : 라우팅 판단의 확신도 0.0~1.0. 질문이 모호하면 낮춰라.
"""


# ─── few-shot (경계 사례 위주) ────────────────────────────────────

FEW_SHOTS = [
    {
        "query": "그룹웨어 리뉴얼 프로젝트 지금 상태 어때? 위험한 거 있으면 알려줘",
        "ui": "screen=project_detail, project_id=1001",
        "answer": {
            "mode": "analysis",
            "domains": ["project"],
            "focus": ["risk"],
            "objective": "그룹웨어 리뉴얼 프로젝트의 현재 진행 상태와 위험 요인을 파악한다",
            "entities": {"project_ids": [1001]},
            "constraints": [],
            "reasoning": "현재 상태 파악 요청이라 analysis. 프로젝트 도메인이면 충분하고 '위험한 거'가 명시되어 risk 를 강조점으로 둔다.",
            "confidence": 0.9,
        },
    },
    {
        "query": "이민주 대리가 8월 초에 휴가 가는데 프로젝트 일정 괜찮을까?",
        "ui": "screen=project_detail, project_id=1001",
        "answer": {
            "mode": "analysis",
            "domains": ["vacation"],
            "focus": [],
            "objective": "이민주의 8월 초 부재가 프로젝트 일정에 주는 영향을 확인한다",
            "entities": {
                "project_ids": [1001],
                "user_names": ["이민주"],
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
            },
            "constraints": [],
            "reasoning": "특정 인원의 휴가가 프로젝트에 주는 영향 확인이라 vacation 하나로 충분하다 — 이 도메인 워커가 마감 겹침·팀원 휴가 겹침을 이미 다 본다. project/hcm 전체 워커까지 돌릴 필요는 없다. 아직 계획을 바꾸자는 게 아니라 영향 확인이라 analysis.",
            "confidence": 0.85,
        },
    },
    {
        "query": "나 다음달에 2일 정도 휴가 쓰고 싶은데 프로젝트에 지장없게 날짜 추천해줘",
        "ui": "screen=project_detail, project_id=1001",
        "answer": {
            "mode": "analysis",
            "domains": ["vacation"],
            "focus": [],
            "objective": "다음 달 중 프로젝트에 지장이 없는 2일짜리 휴가 날짜를 추천한다",
            "entities": {
                "project_ids": [1001],
                "date_from": "2026-09-01",
                "date_to": "2026-09-30",
            },
            "constraints": ["휴가 기간 2일"],
            "reasoning": "구체적 날짜 없이 안전한 날짜 추천을 요청했으므로 vacation 하나로 충분하다. '다음달'은 오늘 날짜 기준으로 다음 달 전체 범위로 환산한다.",
            "confidence": 0.8,
        },
    },
    {
        "query": "예산이 얼마 안 남았는데 남은 작업 중에 뭐부터 해야 할까?",
        "ui": "screen=project_detail, project_id=1002",
        "answer": {
            "mode": "analysis",
            "domains": ["project"],
            "focus": ["cost", "priority"],
            "objective": "남은 예산 제약 아래에서 잔여 작업의 우선순위를 정한다",
            "entities": {"project_ids": [1002]},
            "constraints": ["남은 예산 안에서 해결"],
            "reasoning": "비용 제약 하의 우선순위 질문. project 도메인 하나로 충분하고 cost 와 priority 를 강조한다.",
            "confidence": 0.88,
        },
    },
    {
        "query": "목표일을 2주 앞당겨야 할 것 같아. 어떻게 조정하면 좋을지 안 몇 개 뽑아줘",
        "ui": "screen=project_detail, project_id=1001",
        "answer": {
            "mode": "replan",
            "domains": ["project", "hcm"],
            "focus": [],
            "objective": "목표일을 2주 앞당기는 조정안을 만들어 비교한다",
            "entities": {"project_ids": [1001]},
            "constraints": ["목표일 2주 단축"],
            "reasoning": "제약(마감)이 바뀌어 계획 자체를 다시 짜야 하고 '안 몇 개'를 요구했으므로 replan. 일정 재배치는 인력 재배치를 동반해 hcm 도 필요하다.",
            "confidence": 0.92,
        },
    },
    {
        "query": "AI 에이전트 도입 프로젝트에 누구를 더 붙이면 좋을까?",
        "ui": "screen=project_detail, project_id=1003",
        "answer": {
            "mode": "analysis",
            "domains": ["hcm"],
            "focus": ["skill_fit"],
            "objective": "AI 에이전트 도입 프로젝트에 투입할 적임자를 추천한다",
            "entities": {"project_ids": [1003]},
            "constraints": [],
            "reasoning": "사람 배치 질문이라 hcm 만으로 충분하다. 프로젝트 진척/비용 분석은 필요 없다.",
            "confidence": 0.87,
        },
    },
    {
        "query": "다음 주에 팀 회의 언제 잡는 게 좋을까?",
        "ui": "screen=calendar",
        "answer": {
            "mode": "analysis",
            "domains": ["meeting"],
            "focus": [],
            "objective": "다음 주 팀 회의에 적합한 시간대를 찾는다",
            "entities": {},
            "constraints": [],
            "reasoning": "회의 시간대 탐색이라 meeting 도메인만 해당한다. 프로젝트 분석까지 돌릴 필요가 없다.",
            "confidence": 0.9,
        },
    },
]


def build_few_shot_text() -> str:
    """few-shot 을 프롬프트에 넣을 텍스트로 직렬화."""
    import json

    blocks = []
    for shot in FEW_SHOTS:
        blocks.append(
            f"[질문] {shot['query']}\n"
            f"[화면] {shot['ui']}\n"
            f"[정답] {json.dumps(shot['answer'], ensure_ascii=False)}"
        )
    return "다음은 라우팅 예시다.\n\n" + "\n\n".join(blocks)


# ─── 사용자 메시지 템플릿 ─────────────────────────────────────────

USER_TEMPLATE = """\
{few_shots}

---
이제 실제 요청이다.

[질문] {query}
[화면] {ui_context}
[요청자] {requester}
[오늘] {as_of}
{history}
위 예시와 같은 기준으로 라우팅하라."""
