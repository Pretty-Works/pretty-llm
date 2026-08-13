"""
사용자 도구 — 내 정보(user.me) · 사내 사용자 검색(user.search)

user.me 가 특별한 이유: 응답의 today 가 **상대 날짜 계산의 유일한 기준**이다.
LLM 은 오늘이 며칠인지 모르므로, "어제"·"다음 주 화요일" 같은 표현이 섞인
요청은 다른 어떤 도구보다 이걸 먼저 불러야 한다 (카탈로그 §4-1).
"""

from __future__ import annotations

from langchain.tools import ToolRuntime, tool

from app.clients.backend import backend
from app.tools.registry import RunContext


@tool
async def user_me(runtime: ToolRuntime[RunContext]) -> str:
    """내 정보와 서버 기준 오늘 날짜를 조회한다.

    "어제"·"다음 주 화요일"·"이번 주까지" 처럼 상대 날짜가 섞인 요청이면
    다른 도구보다 이걸 **가장 먼저** 불러 오늘 날짜를 확인한다.
    날짜를 추측하지 마라 — 계산 기준은 이 응답의 today 뿐이다.
    """
    r = await backend.get("/me", run_id=runtime.context.run_id)

    # 요일→날짜를 전부 펼쳐 준다 — LLM 의 요일 산수는 자주 틀리므로
    # (실측: "다음 주 화요일"을 이틀 당겨 계산) 계산 자체를 없앤다.
    from datetime import date, timedelta
    start = date.fromisoformat(r["thisWeekStart"])
    days = ["월", "화", "수", "목", "금", "토", "일"]
    this_week = " ".join(f"{days[i]}={(start + timedelta(i)).isoformat()}" for i in range(7))
    next_week = " ".join(f"{days[i]}={(start + timedelta(7 + i)).isoformat()}" for i in range(7))

    # ★ 8/13 추가 — 요일표만으로는 "내일"을 못 맞힌다. 요일표를 쓰려면 LLM 이
    #   "오늘=목 → 내일은 금 → 표에서 금을 찾기" 2단계를 거쳐야 하는데 그 중간에
    #   틀린다(실측: 오늘이 목 8/13 인데 "내일 오후 2시"를 8/13 으로 등록).
    #   요일표와 같은 원칙으로 이 표현들도 계산 없이 바로 고르게 펼쳐 둔다.
    today = date.fromisoformat(r["today"])
    relative = " ".join(
        f"{label}={(today + timedelta(delta)).isoformat()}"
        for label, delta in (("그저께", -2), ("어제", -1), ("오늘", 0),
                             ("내일", 1), ("모레", 2), ("글피", 3))
    )

    return (f"[{r['userId']}] {r['name']} · {r['department']} {r['position']} · "
            f"프로젝트 생성 권한: {'있음' if r.get('canCreateProject') else '없음'}\n"
            f"오늘: {r['today']} ({r['todayDayOfWeek']})\n"
            f"기준일: {relative}\n"
            f"이번 주: {this_week}\n다음 주: {next_week}\n"
            f"(상대 날짜는 반드시 이 표에서 골라 쓰고, 직접 계산하지 마세요)")


@tool
async def user_search(keyword: str, runtime: ToolRuntime[RunContext]) -> str:
    """이름으로 사내 사용자를 찾아 userId 로 변환한다 (전사 범위).

    일정 참가자처럼 프로젝트 밖 사람을 찾을 때 쓴다.
    프로젝트 관련(회의록 참석자 등)이면 project_members 가 오탐이 적다.
    결과가 여러 명(동명이인)이면 임의로 고르지 말고 부서를 보여주며 되물어라.

    keyword: 이름 (부분 일치, 1~20자)
    """
    r = await backend.get("/users", run_id=runtime.context.run_id, keyword=keyword)
    if not r["users"]:
        return f"'{keyword}' 이름의 재직자가 없습니다. 이름을 다시 확인해 주세요."
    lines = [f"- [{u['userId']}] {u['name']} · {u['department']} {u['position']}"
             + (" (본인)" if u.get("isMe") else "") for u in r["users"]]
    note = " ⚠️ 동명이인 있음 — 임의 선택 금지, 부서로 확인" if r["totalCount"] >= 2 else ""
    return f"사용자 {r['totalCount']}명:{note}\n" + "\n".join(lines)
