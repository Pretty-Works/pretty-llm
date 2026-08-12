# app/workers/me/my_week.py
"""me / my_week 워커 — 내 이번 주, 뭐부터 할까.

★ 2026-08-11 신설. 배포 사고가 난 질문이 원래 갔어야 할 경로다.

  "할 일도 많고 휴가도 신청해야하고 업무도 해야하는데 뭐부터 하는게 좋을까"
  → 전부 본인 스코프 질문인데 프로젝트 분석으로 끌려갔고, 후보를 못 찾자
    전사 명부를 긁어 남의 이름이 답변에 실렸다.

  이 축이 쓰는 내부도구는 `/me`, `/tasks`(본인 주간), `/schedules`(본인),
  `/leaves/balance`(본인) 넷뿐이다. 남의 데이터가 섞일 경로가 없다.
"""

from pydantic import Field

from app.prompts import my_week as prompt
from app.schemas.lenient import LenientModel
from app.workers.base import WorkerSpec


class TaskOrder(LenientModel):
    task_id: int = 0
    title: str = ""
    due_date: str | None = Field(default=None, description="yyyy-MM-dd. 컨텍스트 값 그대로")
    is_overdue: bool = False
    reason: str = Field(default="", description="이 순서인 이유. 날짜로 설명할 것")


class MyWeekResult(LenientModel):
    week_summary: str = Field(default="", description="두 문장 이내. 셀 수 있는 것으로")
    order: list[TaskOrder] = Field(default_factory=list, description="처리 순서")
    leave_advice: str = Field(
        default="", description="휴가를 언급했을 때만. 마감 없는 날과 그 근거"
    )
    notes: list[str] = Field(default_factory=list, description="확인하지 못한 것")


SPEC = WorkerSpec(
    domain="me",
    dimension="my_week",
    role=prompt.ROLE,
    method=prompt.METHOD,
    result_model=MyWeekResult,
    # 도구를 주지 않는다 — 컨텍스트가 곧 전부이고, 더 찾아 헤맬 곳이 없어야 한다.
    tools=(),
    context_sections=("my_week",),
)
