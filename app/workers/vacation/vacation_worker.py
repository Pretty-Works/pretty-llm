# app/workers/vacation/vacation_worker.py
"""vacation / impact 워커 — 휴가가 프로젝트에 주는 영향 + 대안 날짜 추천.

"나 내일 연차 쓸건데 프로젝트에 지장 없어?", "다음달에 2일 휴가 쓰고 싶은데
지장없게 날짜 추천해줘" 같은, 실행이 아니라 분석 결과 자체가 최종 답인 휴가
질문을 담당한다. 단순 실행(그냥 "연차 신청해줘")은 여기로 안 오고 engine_a의
leave_agent(app/engine_a/leave_agent.py)가 처리한다 — classify()가 실행
동사가 있으면 engine_a로 보내는 기존 규칙 그대로다.

이 워커에는 쓰기 도구가 하나도 없다 — 신청/승인은 이 워커의 몫이 아니라서
아예 leave_create/leave_update 를 import 하지 않는다(다른 워커 실수로도 못
새어 들어오게, import 자체를 안 하는 게 제일 확실한 차단이다).

hcm/workload 워커와 필요한 데이터(승인된 휴가, 부하 지표)가 겹치지만 관점이
다르다 — workload 는 "팀 전체에서 누가 과부하인가"를 팀 단위로 훑고, 이
워커는 "이 사람이 이 기간에 쉬면 무엇이 막히는가 / 언제 쉬는 게 안전한가"를
개인 요청 단위로 본다. 그래서 domains=["vacation"] 하나만으로 충분하고,
"혹시 몰라서" hcm/project 를 같이 돌릴 필요는 없다(analysis_router.py 의
"필요한 도메인만 고른다" 원칙) — 필요한 컨텍스트(project/todos/members/
leaves/workload)는 context_builder.build_context() 가 domains 에 "vacation"이
있는 것만으로 이미 다 채워준다.
"""

from typing import Literal

from pydantic import Field

from app.prompts import vacation as prompt
from app.schemas.lenient import LenientModel
from app.tools.hr_tool import find_user, list_member_leaves
from app.tools.project_query import list_project_tasks
from app.workers.base import WorkerSpec


class VacationConflict(LenientModel):
    date: str = Field(default="", description="겹치는 날짜 (YYYY-MM-DD)")
    kind: Literal["deadline", "sole_owner", "team_overlap", "other"] = "deadline"
    subject: str = Field(
        default="", description="지목 대상. 'todo:101', 'user:2' 처럼 식별자로"
    )
    detail: str = Field(default="", description="구체적으로 뭐가 겹치는지 1문장")
    severity: Literal["blocking", "caution", "minor"] = "caution"


class VacationWindowRecommendation(LenientModel):
    start_date: str = ""
    end_date: str = ""
    reason: str = Field(default="", description="왜 이 날짜가 안전한지")
    residual_risk: str = Field(
        default="", description="그래도 남는 우려. 없으면 빈 문자열"
    )


class VacationImpactResult(LenientModel):
    requester_id: int = 0
    requested_start: str | None = None
    requested_end: str | None = None
    verdict: Literal["clear", "caution", "blocking"] = "clear"
    conflicts: list[VacationConflict] = Field(default_factory=list)
    recommended_windows: list[VacationWindowRecommendation] = Field(
        default_factory=list,
        description="구체적 날짜 추천이 필요할 때만 채운다(예: '날짜 추천해줘'). "
                    "이미 날짜가 정해진 질문이면 빈 배열로 둔다.",
    )
    coverage_notes: str = Field(
        default="", description="다른 팀원이 커버 가능한지 참고용 한 문단. 없으면 빈 문자열"
    )


SPEC = WorkerSpec(
    domain="vacation",
    dimension="impact",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=VacationImpactResult,
    tools=(find_user, list_member_leaves, list_project_tasks),
    context_sections=("project", "milestones", "todos", "members", "leaves", "workload"),
)
