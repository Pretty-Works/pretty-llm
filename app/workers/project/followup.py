# app/workers/project/followup.py
"""project / followup 워커 — 회의에서 하기로 한 것이 실제로 굴러가고 있는가.

★ 2026-08-11 신설. 두 가지를 동시에 해결한다.

  1) 회의록(`/projects/{id}/meetings`)은 BE 가 주는데 Engine B 가 한 번도 안 쓰던
     데이터였다. 마일스톤과 함께 붙였지만 그것만으로는 컨텍스트에 실릴 뿐이라
     아무도 그걸 주 근거로 보지 않았다.
  2) skill_fit 이 개인 평가를 빼면서 얇아졌다. 그 자리를 개인정보와 무관하면서
     실무적으로 쓸모 있는 분석으로 메운다.

'회의만 하고 넘어간 항목'은 실제로 프로젝트가 새는 지점인데 지금까지 아무 축도 안 봤다.
근거는 회의록 후속조치 + 프로젝트 할 일 둘뿐이고, 둘 다 프로젝트 화면에 보이는 문서다.
"""

from typing import Literal

from pydantic import Field

from app.prompts import followup as prompt
from app.schemas.lenient import LenientModel
from app.tools.project_query import list_project_tasks
from app.workers.base import WorkerSpec

FollowUpStatus = Literal["TRACKED", "UNTRACKED", "STALLED"]


class FollowUpItem(LenientModel):
    """회의에서 나온 실행 항목 1건."""

    what: str = Field(default="", description="하기로 한 것. 회의록 표현을 그대로 옮기지 말고 한 줄로")
    meeting_id: int = 0
    meeting_date: str | None = None
    status: FollowUpStatus = "UNTRACKED"
    matched_task_ids: list[int] = Field(
        default_factory=list, description="대응하는 할 일 id. 컨텍스트에 있는 것만"
    )
    note: str = Field(default="", description="이 판단의 근거. 회의록 어느 문장인지")


class FollowUpResult(LenientModel):
    summary: str = Field(default="", description="한 줄. 셀 수 있는 것으로")
    items: list[FollowUpItem] = Field(
        default_factory=list, description="UNTRACKED·STALLED 를 우선해서 담는다"
    )
    tracked_count: int = Field(default=0, description="정상 추적 중인 항목 수")


SPEC = WorkerSpec(
    domain="project",
    dimension="followup",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=FollowUpResult,
    tools=(list_project_tasks,),
    context_sections=("project", "meetings", "todos"),
)
