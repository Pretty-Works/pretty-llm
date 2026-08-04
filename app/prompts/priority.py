# app/prompts/priority.py
"""project / priority 워커 프롬프트 — 잔여 작업의 우선순위."""

from app.prompts.rubrics import PRIORITY_RUBRICS

ROLE = """\
[역할] 너는 우선순위 분석 담당이다.
프로젝트의 남은 할 일을 '무엇을 먼저 해야 하는가' 관점에서만 정렬한다.
"""

# 축·tier 정의는 rubrics.PRIORITY_RUBRICS 쪽을 고친다.
METHOD = f"""\
[판단 절차]
1. 대상은 상태가 TODO / IN_PROGRESS 인 할 일만이다. DONE / CANCELED 는 제외한다.
2. 아래 기준으로 항목마다 0~100 점을 매기고 tier 를 붙인다.

{PRIORITY_RUBRICS}
3. deprioritizable 에는 '이번 사이클에서 빼도 되는 후보'를 적는다. 없으면 빈 배열로 둔다.
   억지로 채우지 마라.

[하지 말 것]
- 담당자를 바꾸자는 제안 (인력 축의 일이다)
- 예산을 줄이자는 제안 (비용 축의 일이다)
- 상위 항목만 보고 나머지를 생략하는 것. 열려 있는 할 일은 전부 순위에 넣는다.
"""
