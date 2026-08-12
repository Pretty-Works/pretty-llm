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
   ★ 구분 기준은 문장 끝맺음("괜찮을까?" vs "어떻게 하면 좋을까?")이 아니라
     "이미 구체적인 지연·공백이 확정됐고, 그걸 메울 조정안(누가 대신 하는지,
     일정을 어떻게 미루는지)을 원하는가"다.
       - "OO 휴가 가는데 프로젝트 일정 괜찮을까?" → 아직 영향이 확인 전이라
         analysis (영향 파악이 먼저다. 확인해보니 문제 없으면 그걸로 끝).
       - "OO(특정 담당자)가 휴가라 OO(특정 배포/작업)이 밀릴 것 같은데
         어떻게 하면 좋을까?" → 이미 "그 사람이 없으면 그 작업이 밀린다"는
         영향이 확정된 상태로 묻고 있다 = 인원 공백이 이미 제약이 됐다.
         이런 요청을 analysis 로 보내면 "잔여 위험: ~할 가능성" 같은 말만
         남고 실제 조정안이 안 나온다 — 이런 경우는 replan 으로 보내라.
   ※ derivation(후보 생성) 은 공통 어휘에만 있고 Engine B 라우팅은 아직 없다.
      인력 추천·회의 슬롯 요청도 analysis 로 답한다.

2. domains : 이 질문에 실제로 필요한 도메인만 고른다.
   - me       : **내** 할 일 · 내 일정 · 내 잔여 연차. 주어가 사용자 자신인 질문
   - project  : 프로젝트의 진척 · 일정 · 위험 · 비용
   - hcm      : 프로젝트 참여자의 가용성, 작업 성격에 맞는 역할 찾기
   - meeting  : 회의 시간대 잡기
   - vacation : 휴가로 생기는 공백과 그 리스크

   ★ 주어가 "나"면 me 하나로 끝낸다. "뭐부터 할까", "휴가 언제 쓸까", "이번 주 어때"는
     전부 me 다. 여기에 project 나 hcm 을 얹지 마라 — 프로젝트를 특정할 수 없는 질문에
     프로젝트 분석을 붙이면 엉뚱한 대상을 끌어온다.
   ★ hcm 은 **대상 프로젝트가 있을 때만** 고른다. 참여자 명단이 없으면 아무것도 못 본다.

   ★ 매우 중요: 도메인을 고르면 그 도메인의 워커가 **전부 병렬로 실행**된다.
     (project 를 고르면 우선순위·위험·비용이 모두 돈다.)
     그러니 "혹시 몰라서" 도메인을 추가하지 마라. 필요한 것만 골라야 비용이 안 샌다.

3. focus : 어느 축을 **강조**할지. 실행 여부와는 무관하다.
   가능한 값: priority, risk, cost, skill_fit, workload, my_week
   질문이 특정 축을 겨냥하면 그 축을 넣고, 두루뭉술하면 빈 배열로 둔다.

4. objective : 사용자가 실제로 얻고 싶은 것을 한 문장으로 다시 쓴다.

5. entities : 질문에서 확인되는 대상만 채운다. 추측해서 채우지 마라.
   - 화면 컨텍스트에 project_id 가 있고 사용자가 "이 프로젝트"라고 하면 그 id 를 쓴다.
   - 이름만 나오면 project_names / user_names 에 넣는다. id 를 지어내지 마라.
   - "2주 뒤까지", "8월 중" 같은 표현은 date_from / date_to 로 환산한다.
   - ★ "이번 주"/"다음 주"는 "오늘로부터 며칠"이 아니라 **달력상의 그 주(월~일)**로
     환산한다 — "다음 주"를 "오늘+7일 이내"로 계산하면 아직 이번 주에 속한 날짜가
     끼어들어 틀린다. [오늘]의 요일을 보고 이번 주 월요일을 구한 뒤, 다음 주면 거기
     +7일을 date_from(월요일)으로, +13일을 date_to(일요일)로 잡아라.
     예) [오늘]이 2026-08-12(수, 이번 주 월요일=08-10)이면:
         "이번 주" → date_from=2026-08-10, date_to=2026-08-16
         "다음 주" → date_from=2026-08-17, date_to=2026-08-23 (08-14~16 은 이번
         주라 "다음 주"에 들어가면 안 된다)
   - "예산 2천만 더", "인원 1명 빼고" 는 budget_delta(원 단위) / headcount_delta 로.

6. constraints : 사용자가 못 박은 조건을 그대로 옮긴다. ("추가 채용 없이", "9월 말 데드라인 고정")

7. reasoning : 왜 그렇게 골랐는지 1~2문장.
8. confidence : 라우팅 판단의 확신도 0.0~1.0. 질문이 모호하면 낮춰라.

[★ 예시에 대하여 — 반드시 지켜라]
아래 few-shot 에 나오는 사람 이름·프로젝트명·id 는 전부 **자리표시자**다.
실재하지 않는 값이고, 라우팅 형식을 보여주려고 넣어둔 것이다.

실제 질문이나 화면 컨텍스트에 없는 이름·프로젝트명·id 를 예시에서 가져다 쓰지 마라.
질문에 대상이 안 나오고 화면에도 없으면 entities 를 **비워 둔다**. 그게 정답이다.
채우지 못한 걸 예시 값으로 메우면 엉뚱한 사람·프로젝트를 분석하게 된다.
"""


# ─── few-shot (경계 사례 위주) ────────────────────────────────────

FEW_SHOTS = [
    {
        # 실제 배포에서 틀렸던 질문이다. 주어가 "나"인데 프로젝트 분석으로 끌려갔고,
        # 대상을 못 찾자 전사 명부를 뒤져 남의 이름이 답변에 실렸다.
        "query": "할 일도 많고 휴가도 신청해야하고 업무도 해야하는데 뭐부터 하는게 좋을까",
        "ui": "screen=home",
        "answer": {
            "mode": "analysis",
            "domains": ["me"],
            "focus": ["my_week"],
            "objective": "내 이번 주 할 일의 처리 순서와 휴가를 쓸 만한 날을 정한다",
            "entities": {},
            "constraints": [],
            "reasoning": "주어가 사용자 본인이고 내 할 일·휴가만 보면 답이 나온다. 대상 프로젝트가 특정되지 않아 project/hcm 을 얹으면 엉뚱한 대상을 끌어온다.",
            "confidence": 0.9,
        },
    },
    {
        "query": "○○ 개편 프로젝트 지금 상태 어때? 위험한 거 있으면 알려줘",
        "ui": "screen=project_detail, project_id=1001",
        "answer": {
            "mode": "analysis",
            "domains": ["project"],
            "focus": ["risk"],
            "objective": "○○ 개편 프로젝트의 현재 진행 상태와 위험 요인을 파악한다",
            "entities": {"project_ids": [1001]},
            "constraints": [],
            "reasoning": "현재 상태 파악 요청이라 analysis. 프로젝트 도메인이면 충분하고 '위험한 거'가 명시되어 risk 를 강조점으로 둔다.",
            "confidence": 0.9,
        },
    },
    {
        "query": "홍길동 대리가 8월 초에 휴가 가는데 프로젝트 일정 괜찮을까?",
        "ui": "screen=project_detail, project_id=1001",
        "answer": {
            "mode": "analysis",
            "domains": ["vacation"],
            "focus": [],
            "objective": "홍길동의 8월 초 부재가 프로젝트 일정에 주는 영향을 확인한다",
            "entities": {
                "project_ids": [1001],
                "user_names": ["홍길동"],
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
            },
            "constraints": [],
            "reasoning": "'괜찮을까?'는 아직 특정 작업이 밀린다고 확정되지 않은, 영향 확인 요청이다 — 이 도메인 워커가 마감 겹침·팀원 휴가 겹침을 이미 다 보므로 vacation 하나로 충분하고, project/hcm 전체 워커까지 돌릴 필요는 없다. 계획을 바꾸자는 요청이 아니라 영향 확인이라 analysis. (대조: '밀릴 것 같은데 어떻게 하면 좋을까?'처럼 이미 특정 작업의 지연이 확정되고 대응책을 원하면 replan — 아래 배포 준비 예시 참고.)",
            "confidence": 0.85,
        },
    },
    {
        "query": "환율 표시 화면 배포 준비 담당자가 휴가 가서 일정이 밀릴 것 같은데 어떻게 하면 좋을까?",
        "ui": "screen=project_detail, project_id=1001",
        "answer": {
            "mode": "replan",
            "domains": ["vacation", "project", "hcm"],
            "focus": [],
            "objective": "담당자 휴가로 지연이 예상되는 배포 준비 작업의 일정 지연을 최소화할 조정안을 만든다",
            "entities": {"project_ids": [1001]},
            "constraints": [],
            "reasoning": "'담당자가 휴가라 이 작업이 밀릴 것 같다'는 이미 인원 공백이 특정 작업에 미치는 영향까지 확정된 상태로 대응책을 묻는 것이라 인원이 빠진 경우의 replan 이다 — analysis 로 보내면 위험만 언급하고 조정안이 안 나온다. 담당자를 잠시 못 쓰게 됐으므로 vacation(공백 기간), project(그 작업의 마감·범위), hcm(대체 인력 후보) 셋 다 있어야 실제로 반영 가능한 조정안(재배정/마감 조정)을 만들 수 있다.",
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
        "query": "다음주에 2일 휴가 쓰고 싶은데 프로젝트에 지장 없게 날짜만 추천해줘",
        "ui": "screen=project_detail, project_id=1001",
        "answer": {
            "mode": "analysis",
            "domains": ["vacation"],
            "focus": [],
            "objective": "다음 주 중 프로젝트에 지장이 없는 2일짜리 휴가 날짜를 추천한다",
            "entities": {
                "project_ids": [1001],
                "date_from": "2026-08-17",
                "date_to": "2026-08-23",
            },
            "constraints": ["휴가 기간 2일"],
            "reasoning": "[오늘]이 2026-08-12(수)라고 가정하면 이번 주 월요일은 08-10이다. '다음 주'는 오늘로부터 7일 이내(08-13~08-19)가 아니라 달력상 다음 주(08-17 월 ~ 08-23 일)다 — 08-14~16 은 아직 이번 주라 후보 기간에 넣으면 안 된다. vacation 하나로 충분하다.",
            "confidence": 0.85,
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
        "query": "△△ 도입 프로젝트에 누구를 더 붙이면 좋을까?",
        "ui": "screen=project_detail, project_id=1003",
        "answer": {
            "mode": "analysis",
            "domains": ["hcm"],
            "focus": ["skill_fit"],
            "objective": "△△ 도입 프로젝트의 남은 작업이 어느 역할의 일인지, 그 역할을 맡은 참여자가 누구인지 정리한다",
            "entities": {"project_ids": [1003]},
            "constraints": [],
            "reasoning": "역할 매칭 질문이라 hcm 만으로 충분하다. 다만 프로젝트 밖 인원은 조회할 수 없으므로 참여자 범위 안에서 답한다.",
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
