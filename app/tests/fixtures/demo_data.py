# app/tools/demo_data.py
"""데모용 인메모리 데이터셋 (백엔드 미가동 시 폴백).

`BACKEND_BASE_URL` 이 설정되면 tool 들이 실제 API 를 호출하고, 아니면 여기를 읽는다.
스키마는 ERD 설계 문서의 테이블(users / projects / project_relation / todos /
결재_휴가 / 일정 / 결재)을 그대로 따랐다.

기준일은 2026-07-27 근처를 가정했다. (.env 의 AS_OF 로 고정 가능)
"""

from datetime import date
from typing import Any

from app.utils.parser import overlaps, parse_date


# ─── users ────────────────────────────────────────────────────────

USERS: list[dict[str, Any]] = [
    {"id": 1, "employee_no": "20180302", "name": "김수민", "department": "개발팀", "position": "팀장", "hire_date": "2018-03-02", "status": "ACTIVE", "email": "sumin@pretty.works"},
    {"id": 2, "employee_no": "20210701", "name": "이민주", "department": "개발팀", "position": "대리", "hire_date": "2021-07-01", "status": "ACTIVE", "email": "minju@pretty.works"},
    {"id": 3, "employee_no": "20240115", "name": "박지원", "department": "개발팀", "position": "사원", "hire_date": "2024-01-15", "status": "ACTIVE", "email": "jiwon@pretty.works"},
    {"id": 4, "employee_no": "20150511", "name": "정우진", "department": "기획팀", "position": "부장", "hire_date": "2015-05-11", "status": "ACTIVE", "email": "woojin@pretty.works"},
    {"id": 5, "employee_no": "20190902", "name": "최유나", "department": "디자인팀", "position": "과장", "hire_date": "2019-09-02", "status": "ACTIVE", "email": "yuna@pretty.works"},
    {"id": 6, "employee_no": "20250302", "name": "한도윤", "department": "개발팀", "position": "사원", "hire_date": "2025-03-02", "status": "ACTIVE", "email": "doyun@pretty.works"},
    {"id": 7, "employee_no": "20220418", "name": "오세라", "department": "QA팀", "position": "대리", "hire_date": "2022-04-18", "status": "ACTIVE", "email": "sera@pretty.works"},
    {"id": 8, "employee_no": "20171106", "name": "서준호", "department": "인프라팀", "position": "과장", "hire_date": "2017-11-06", "status": "ACTIVE", "email": "junho@pretty.works"},
]


# ─── projects / project_relation ──────────────────────────────────

PROJECTS: list[dict[str, Any]] = [
    {"id": 1000, "name": "인사시스템 고도화", "status": "COMPLETED", "start_date": "2025-09-01", "due_date": "2026-03-31"},
    {"id": 1001, "name": "그룹웨어 리뉴얼", "status": "ACTIVE", "start_date": "2026-04-01", "due_date": "2026-09-30"},
    {"id": 1002, "name": "사내 메일 Outlook 연동", "status": "ACTIVE", "start_date": "2026-06-15", "due_date": "2026-08-31"},
    {"id": 1003, "name": "AI 에이전트 도입", "status": "PLANNING", "start_date": "2026-08-01", "due_date": "2026-12-31"},
    # ★ 재계획 시연용 — 백엔드 리드 1명이 핵심 태스크 2개를 쥔 채 정확히 그 마감
    #   기간에 승인 휴가가 겹치는 시나리오. EXTEND(연장)/REALLOCATE(인력 재배치)/
    #   REDUCE_SCOPE(범위 축소) 3안이 전부 근거를 갖고 갈리도록 설계했다.
    {"id": 1004, "name": "AI 코파일럿 베타 출시", "status": "ACTIVE", "start_date": "2026-07-01", "due_date": "2026-09-15"},
]

PROJECT_MEMBERS: list[dict[str, Any]] = [
    # p000 (완료) - skill 추론용 과거 이력
    {"project_id": 1000, "user_id": 1, "role": "PM", "status": "ACTIVE"},
    {"project_id": 1000, "user_id": 2, "role": "BE", "status": "ACTIVE"},
    {"project_id": 1000, "user_id": 6, "role": "FE", "status": "ACTIVE"},
    {"project_id": 1000, "user_id": 7, "role": "QA", "status": "ACTIVE"},
    {"project_id": 1000, "user_id": 8, "role": "INFRA", "status": "ACTIVE"},
    # p001
    {"project_id": 1001, "user_id": 1, "role": "PM", "status": "ACTIVE"},
    {"project_id": 1001, "user_id": 2, "role": "BE", "status": "ACTIVE"},
    {"project_id": 1001, "user_id": 3, "role": "FE", "status": "ACTIVE"},
    {"project_id": 1001, "user_id": 5, "role": "DESIGN", "status": "ACTIVE"},
    {"project_id": 1001, "user_id": 7, "role": "QA", "status": "ACTIVE"},
    # p002
    {"project_id": 1002, "user_id": 2, "role": "PM", "status": "ACTIVE"},
    {"project_id": 1002, "user_id": 6, "role": "BE", "status": "ACTIVE"},
    {"project_id": 1002, "user_id": 8, "role": "INFRA", "status": "ACTIVE"},
    # p003
    {"project_id": 1003, "user_id": 4, "role": "PM", "status": "ACTIVE"},
    {"project_id": 1003, "user_id": 1, "role": "BE", "status": "ACTIVE"},
    {"project_id": 1003, "user_id": 6, "role": "BE", "status": "ACTIVE"},
    {"project_id": 1003, "user_id": 7, "role": "QA", "status": "ACTIVE"},
    # p004 — ★ BE 는 김수민(1) 혼자다(SPOF). 한도윤(6)·서준호(8)는 프로젝트 밖
    #   인원이라 skill_fit 워커가 "적임자 추천" 후보군(전사 인원)에서 찾아야 한다.
    {"project_id": 1004, "user_id": 4, "role": "PM", "status": "ACTIVE"},
    {"project_id": 1004, "user_id": 1, "role": "BE", "status": "ACTIVE"},
    {"project_id": 1004, "user_id": 3, "role": "FE", "status": "ACTIVE"},
    {"project_id": 1004, "user_id": 7, "role": "QA", "status": "ACTIVE"},
]


# ─── todos ────────────────────────────────────────────────────────

TODOS: list[dict[str, Any]] = [
    # p001
    {"id": 101, "project_id": 1001, "title": "권한 정책 재설계", "status": "IN_PROGRESS", "due_date": "2026-07-20", "assignee_id": 2},
    {"id": 102, "project_id": 1001, "title": "결재선 화면 개편", "status": "IN_PROGRESS", "due_date": "2026-08-07", "assignee_id": 3},
    {"id": 103, "project_id": 1001, "title": "회의록 에디터", "status": "TODO", "due_date": "2026-08-21", "assignee_id": 3},
    {"id": 104, "project_id": 1001, "title": "휴가 신청 플로우 QA", "status": "TODO", "due_date": "2026-08-14", "assignee_id": 7},
    {"id": 105, "project_id": 1001, "title": "디자인 시스템 토큰 정리", "status": "DONE", "due_date": "2026-06-30", "assignee_id": 5},
    {"id": 106, "project_id": 1001, "title": "알림 센터 API", "status": "IN_PROGRESS", "due_date": "2026-07-31", "assignee_id": 2},
    {"id": 107, "project_id": 1001, "title": "게시판 첨부 GCS 연동", "status": "TODO", "due_date": "2026-08-28", "assignee_id": 2},
    {"id": 108, "project_id": 1001, "title": "예약 충돌 검증 로직", "status": "TODO", "due_date": "2026-07-31", "assignee_id": 3},
    {"id": 109, "project_id": 1001, "title": "마이페이지 리뉴얼", "status": "DONE", "due_date": "2026-07-10", "assignee_id": 5},
    {"id": 110, "project_id": 1001, "title": "성능 프로파일링", "status": "TODO", "due_date": "2026-09-11", "assignee_id": 1},
    {"id": 111, "project_id": 1001, "title": "접근성 점검", "status": "TODO", "due_date": "2026-09-18", "assignee_id": 5},
    {"id": 112, "project_id": 1001, "title": "1차 통합 테스트", "status": "TODO", "due_date": "2026-08-31", "assignee_id": 7},
    # p002
    {"id": 201, "project_id": 1002, "title": "Outlook OAuth 연동", "status": "IN_PROGRESS", "due_date": "2026-07-31", "assignee_id": 6},
    {"id": 202, "project_id": 1002, "title": "메일 동기화 배치", "status": "TODO", "due_date": "2026-08-10", "assignee_id": 6},
    {"id": 203, "project_id": 1002, "title": "메일함 UI", "status": "TODO", "due_date": "2026-08-18", "assignee_id": 2},
    {"id": 204, "project_id": 1002, "title": "SMTP 릴레이 인프라", "status": "IN_PROGRESS", "due_date": "2026-08-05", "assignee_id": 8},
    {"id": 205, "project_id": 1002, "title": "휴지통/중요메일 처리", "status": "TODO", "due_date": "2026-08-24", "assignee_id": 6},
    {"id": 206, "project_id": 1002, "title": "메일 보안 검토", "status": "TODO", "due_date": "2026-08-28", "assignee_id": 8},
    # p003
    {"id": 301, "project_id": 1003, "title": "요구사항 정의", "status": "IN_PROGRESS", "due_date": "2026-08-14", "assignee_id": 4},
    {"id": 302, "project_id": 1003, "title": "에이전트 PoC", "status": "TODO", "due_date": "2026-09-30", "assignee_id": 1},
    {"id": 303, "project_id": 1003, "title": "평가 데이터셋 구축", "status": "TODO", "due_date": "2026-10-16", "assignee_id": 7},
    {"id": 304, "project_id": 1003, "title": "인프라 비용 산정", "status": "TODO", "due_date": "2026-08-31", "assignee_id": 6},
    # p004 — 핵심(401·402·408)은 전부 김수민(1) 담당. 405·406 은 베타 출시에
    #   안 막히는 후속 기능이라 REDUCE_SCOPE 의 "뺄 후보"로 쓰라고 일부러 넣었다.
    {"id": 401, "project_id": 1004, "title": "추천 엔진 API 연동", "status": "IN_PROGRESS", "due_date": "2026-08-25", "assignee_id": 1},
    {"id": 402, "project_id": 1004, "title": "프롬프트 안전 필터링", "status": "TODO", "due_date": "2026-08-28", "assignee_id": 1},
    {"id": 403, "project_id": 1004, "title": "베타 온보딩 UI", "status": "IN_PROGRESS", "due_date": "2026-08-20", "assignee_id": 3},
    {"id": 404, "project_id": 1004, "title": "사용성 테스트 진행", "status": "TODO", "due_date": "2026-09-05", "assignee_id": 7},
    {"id": 405, "project_id": 1004, "title": "관리자 대시보드 통계 위젯", "status": "TODO", "due_date": "2026-09-10", "assignee_id": 1},
    {"id": 406, "project_id": 1004, "title": "다국어 지원(2차 예정 기능 선반영)", "status": "TODO", "due_date": "2026-09-12", "assignee_id": 3},
    {"id": 407, "project_id": 1004, "title": "베타 피드백 설문지 연동", "status": "TODO", "due_date": "2026-08-30", "assignee_id": 4},
    {"id": 408, "project_id": 1004, "title": "부하 테스트(피크 트래픽 시뮬레이션)", "status": "TODO", "due_date": "2026-09-08", "assignee_id": 1},
]


# ─── 휴가 (결재_휴가 승인분) / 연차현황 ───────────────────────────

LEAVES: list[dict[str, Any]] = [
    {"id": 2001, "user_id": 2, "leave_type": "연차", "start_date": "2026-08-03", "end_date": "2026-08-07", "status": "APPROVED"},
    {"id": 2002, "user_id": 5, "leave_type": "연차", "start_date": "2026-07-29", "end_date": "2026-07-31", "status": "APPROVED"},
    {"id": 2003, "user_id": 6, "leave_type": "연차", "start_date": "2026-08-17", "end_date": "2026-08-18", "status": "APPROVED"},
    {"id": 2004, "user_id": 7, "leave_type": "생일휴가", "start_date": "2026-09-04", "end_date": "2026-09-04", "status": "APPROVED"},
    # 미승인건은 가용성 계산에 반영하지 않는다 (ERD: 승인된 신청서 기준)
    {"id": 2005, "user_id": 3, "leave_type": "연차", "start_date": "2026-08-24", "end_date": "2026-08-28", "status": "PENDING"},
    {"id": 2006, "user_id": 1, "leave_type": "연차", "start_date": "2026-05-06", "end_date": "2026-05-08", "status": "APPROVED"},
    # ★ p004 재계획 시연의 핵심 충돌 — 김수민(1)의 승인 휴가가 401·402 마감과
    #   정면으로 겹친다. risk/skill_fit 워커가 "1순위 추천 불가" 사유로 잡아야 한다.
    {"id": 2007, "user_id": 1, "leave_type": "연차", "start_date": "2026-08-24", "end_date": "2026-08-28", "status": "APPROVED"},
]

# [연차현황] user_id, 연도, 부여일수
LEAVE_GRANTS: list[dict[str, Any]] = [
    {"user_id": 1, "year": 2026, "granted_days": 18},
    {"user_id": 2, "year": 2026, "granted_days": 16},
    {"user_id": 3, "year": 2026, "granted_days": 12},
    {"user_id": 4, "year": 2026, "granted_days": 22},
    {"user_id": 5, "year": 2026, "granted_days": 17},
    {"user_id": 6, "year": 2026, "granted_days": 11},
    {"user_id": 7, "year": 2026, "granted_days": 15},
    {"user_id": 8, "year": 2026, "granted_days": 18},
]


# ─── 일정 (회의 밀도 = 부하 산정 근거) ────────────────────────────

SCHEDULES: list[dict[str, Any]] = [
    {"id": 3001, "project_id": 1001, "title": "주간 스크럼", "date": "2026-07-28", "start_time": "10:00", "end_time": "11:00", "participants": [1, 2, 3, 5, 7]},
    {"id": 3002, "project_id": 1002, "title": "연동 점검", "date": "2026-07-29", "start_time": "14:00", "end_time": "15:30", "participants": [2, 6, 8]},
    {"id": 3003, "project_id": 1001, "title": "설계 리뷰", "date": "2026-07-30", "start_time": "15:00", "end_time": "17:00", "participants": [1, 2, 3]},
    {"id": 3004, "project_id": 1003, "title": "킥오프", "date": "2026-08-03", "start_time": "10:00", "end_time": "12:00", "participants": [4, 1, 6, 7]},
    {"id": 3005, "project_id": 1001, "title": "QA 싱크", "date": "2026-08-06", "start_time": "11:00", "end_time": "12:00", "participants": [7, 3]},
    {"id": 3006, "project_id": 1002, "title": "보안 리뷰", "date": "2026-08-11", "start_time": "14:00", "end_time": "15:00", "participants": [8, 6, 4]},
    {"id": 3007, "project_id": 1001, "title": "주간 스크럼", "date": "2026-08-04", "start_time": "10:00", "end_time": "11:00", "participants": [1, 3, 5, 7]},
    {"id": 3008, "project_id": 1004, "title": "베타 출시 리허설", "date": "2026-08-19", "start_time": "14:00", "end_time": "16:00", "participants": [1, 3, 4, 7]},
    {"id": 3009, "project_id": 1004, "title": "런칭 회고 계획", "date": "2026-09-01", "start_time": "11:00", "end_time": "12:00", "participants": [4, 7]},
]


# ─── 예산 / 지출 / 결재 진행중(committed) ─────────────────────────

# ─── 회의록 (followup 축 근거) ────────────────────────────────────
#
# 후속 조치가 실제 할 일로 이어졌는지 대조하는 축(project.followup)을 돌려보려면
# 회의록이 있어야 한다. t102(결재선 화면 개편)는 대응 할 일이 있고, "권한 매트릭스
# 재검토"는 대응 할 일이 없어 UNTRACKED 로 잡혀야 한다.

MEETINGS: list[dict[str, Any]] = [
    {"id": 5001, "project_id": 1001, "title": "스프린트 리뷰 2차", "date": "2026-07-22",
     "purpose": "진행 점검",
     "content": "결재선 화면 개편 진행률 40%. 권한 정책 재설계가 지연되어 알림 센터 API 착수가 밀림.",
     "follow_up": "결재선 화면 개편 8월 첫째 주까지 마무리하기로 함. "
                  "권한 매트릭스 재검토는 별도로 진행하기로 함.",
     "attendee_names": ["김수민", "이민주", "박지원"]},
    {"id": 5002, "project_id": 1001, "title": "QA 준비 회의", "date": "2026-07-24",
     "purpose": "QA 일정 확정",
     "content": "휴가 신청 플로우 QA 범위 합의. 1차 통합 테스트 일정 공유함.",
     "follow_up": "휴가 신청 플로우 QA 8월 중순까지 완료하기로 함.",
     "attendee_names": ["오세라", "박지원"]},
]


BUDGETS: list[dict[str, Any]] = [
    {"project_id": 1001, "total": 120_000_000, "currency": "KRW"},
    {"project_id": 1002, "total": 40_000_000, "currency": "KRW"},
    {"project_id": 1003, "total": 60_000_000, "currency": "KRW"},
    {"project_id": 1004, "total": 80_000_000, "currency": "KRW"},
]

EXPENSES: list[dict[str, Any]] = [
    {"id": 4101, "project_id": 1001, "title": "외주 UI 개발 1차", "category": "외주", "amount": 42_000_000, "date": "2026-05-20", "status": "PAID"},
    {"id": 4102, "project_id": 1001, "title": "디자인 툴 라이선스", "category": "라이선스", "amount": 8_500_000, "date": "2026-04-15", "status": "PAID"},
    {"id": 4103, "project_id": 1001, "title": "클라우드 사용료(4~7월)", "category": "인프라", "amount": 21_000_000, "date": "2026-07-01", "status": "PAID"},
    {"id": 4104, "project_id": 1001, "title": "사용성 테스트 용역", "category": "외주", "amount": 15_000_000, "date": "2026-06-30", "status": "PAID"},
    {"id": 4201, "project_id": 1002, "title": "Outlook 커넥터 라이선스", "category": "라이선스", "amount": 12_000_000, "date": "2026-06-20", "status": "PAID"},
    {"id": 4202, "project_id": 1002, "title": "메일 동기화 외주 개발", "category": "외주", "amount": 16_200_000, "date": "2026-07-10", "status": "PAID"},
    {"id": 4203, "project_id": 1002, "title": "SMTP 릴레이 인프라", "category": "인프라", "amount": 3_000_000, "date": "2026-07-15", "status": "PAID"},
    # p004 — 예산 자체는 여유 있다(33% 집행). 이 시연의 제약은 돈이 아니라
    # 사람(SPOF)이라는 걸 보여주려는 의도다 — cost 워커가 "예산은 문제 없다"고
    # 판단하는 것도 정상 결과다.
    {"id": 4401, "project_id": 1004, "title": "GPU 추론 인프라(7월)", "category": "인프라", "amount": 14_000_000, "date": "2026-07-25", "status": "PAID"},
    {"id": 4402, "project_id": 1004, "title": "LLM API 사용료(7~8월 누적)", "category": "인프라", "amount": 9_500_000, "date": "2026-08-05", "status": "PAID"},
    {"id": 4403, "project_id": 1004, "title": "베타 테스터 리워드", "category": "마케팅", "amount": 3_000_000, "date": "2026-08-01", "status": "PAID"},
]

# 결재 진행중 = 아직 안 나갔지만 사실상 묶인 금액
PENDING_APPROVALS: list[dict[str, Any]] = [
    {"id": 5101, "project_id": 1001, "title": "9월 클라우드 선결제", "amount": 12_000_000, "status": "PENDING", "doc_type": "지출결의서"},
    {"id": 5201, "project_id": 1002, "title": "메일 보안 점검 외주", "amount": 6_500_000, "status": "PENDING", "doc_type": "품의서"},
    {"id": 5401, "project_id": 1004, "title": "9월 GPU 확장 증설", "amount": 6_000_000, "status": "PENDING", "doc_type": "품의서"},
]


# ─── 접근자 ───────────────────────────────────────────────────────

def list_users(department: str | None = None, position: str | None = None) -> list[dict]:
    rows = USERS
    if department:
        rows = [u for u in rows if u["department"] == department]
    if position:
        rows = [u for u in rows if u["position"] == position]
    return [dict(u) for u in rows]


def get_user(user_id: int | None = None, name: str | None = None) -> dict | None:
    for user in USERS:
        if user_id and user["id"] == user_id:
            return dict(user)
        if name and name in user["name"]:
            return dict(user)
    return None


def get_project(project_id: int | None = None, name: str | None = None) -> dict | None:
    for project in PROJECTS:
        if project_id and project["id"] == project_id:
            return dict(project)
        if name and name in project["name"]:
            return dict(project)
    return None


def list_projects(user_id: int | None = None, status: str | None = None) -> list[dict]:
    rows = PROJECTS
    if status:
        rows = [p for p in rows if p["status"] == status]
    if user_id:
        joined = {m["project_id"] for m in PROJECT_MEMBERS if m["user_id"] == user_id}
        rows = [p for p in rows if p["id"] in joined]
    return [dict(p) for p in rows]


def list_project_members(project_id: int) -> list[dict]:
    """project_relation JOIN users."""
    result = []
    for relation in PROJECT_MEMBERS:
        if relation["project_id"] != project_id:
            continue
        user = get_user(user_id=relation["user_id"])
        if not user:
            continue
        result.append({**user, "role": relation["role"], "project_status": relation["status"]})
    return result


def user_project_history(user_id: int) -> list[dict]:
    """이 사람이 어떤 프로젝트에서 어떤 역할을 했는지. skill 추론의 근거."""
    history = []
    for relation in PROJECT_MEMBERS:
        if relation["user_id"] != user_id:
            continue
        project = get_project(project_id=relation["project_id"])
        if not project:
            continue
        history.append(
            {
                "project_id": project["id"],
                "project_name": project["name"],
                "project_status": project["status"],
                "role": relation["role"],
                "start_date": project["start_date"],
                "due_date": project["due_date"],
            }
        )
    return history


def list_todos(
    project_id: int | None = None,
    assignee_id: int | None = None,
    status: str | None = None,
) -> list[dict]:
    rows = TODOS
    if project_id:
        rows = [t for t in rows if t["project_id"] == project_id]
    if assignee_id:
        rows = [t for t in rows if t["assignee_id"] == assignee_id]
    if status:
        rows = [t for t in rows if t["status"] == status]
    enriched = []
    for todo in rows:
        user = get_user(user_id=todo["assignee_id"])
        enriched.append({**todo, "assignee_name": user["name"] if user else None})
    return enriched


def list_leaves(
    user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = "APPROVED",
) -> list[dict]:
    rows = LEAVES
    if user_id:
        rows = [leave for leave in rows if leave["user_id"] == user_id]
    if status:
        rows = [leave for leave in rows if leave["status"] == status]
    if date_from and date_to:
        rows = [
            leave
            for leave in rows
            if overlaps(
                parse_date(leave["start_date"]),
                parse_date(leave["end_date"]),
                date_from,
                date_to,
            )
        ]
    enriched = []
    for leave in rows:
        user = get_user(user_id=leave["user_id"])
        enriched.append({**leave, "user_name": user["name"] if user else None})
    return enriched


def leave_balance(user_id: int, year: int = 2026) -> dict:
    """ERD 원칙대로 잔여 연차를 저장값이 아니라 '부여일수 - 승인된 휴가일수'로 계산."""
    granted = next(
        (g["granted_days"] for g in LEAVE_GRANTS if g["user_id"] == user_id and g["year"] == year),
        0,
    )
    used = 0
    for leave in LEAVES:
        if leave["user_id"] != user_id or leave["status"] != "APPROVED":
            continue
        start, end = parse_date(leave["start_date"]), parse_date(leave["end_date"])
        if not start or not end or start.year != year:
            continue
        used += (end - start).days + 1
    return {"user_id": user_id, "year": year, "granted": granted, "used": used, "remaining": granted - used}


def list_schedules(
    user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    project_id: int | None = None,
) -> list[dict]:
    rows = SCHEDULES
    if user_id:
        rows = [s for s in rows if user_id in s["participants"]]
    if project_id:
        rows = [s for s in rows if s["project_id"] == project_id]
    if date_from and date_to:
        rows = [
            s
            for s in rows
            if (d := parse_date(s["date"])) and date_from <= d <= date_to
        ]
    return [dict(s) for s in rows]


def get_budget(project_id: int) -> dict | None:
    budget = next((b for b in BUDGETS if b["project_id"] == project_id), None)
    if not budget:
        return None
    spent = sum(e["amount"] for e in EXPENSES if e["project_id"] == project_id)
    committed = sum(
        a["amount"]
        for a in PENDING_APPROVALS
        if a["project_id"] == project_id and a["status"] == "PENDING"
    )
    return {**budget, "spent": spent, "committed": committed}


def list_expenses(project_id: int) -> list[dict]:
    return [dict(e) for e in EXPENSES if e["project_id"] == project_id]


def list_pending_approvals(project_id: int) -> list[dict]:
    return [
        dict(a)
        for a in PENDING_APPROVALS
        if a["project_id"] == project_id and a["status"] == "PENDING"
    ]
