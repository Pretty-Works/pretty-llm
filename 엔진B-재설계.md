# Engine B 재설계 — 권한 경계 안에서 할 수 있는 최대치로

작성 배경: 배포 환경에서 존재하지 않는 직원(이민주·박지원·한도윤)이 분석 결과에 등장했다.
원인은 픽스처 유입이었고, 추적 과정에서 **BE 가 주는 데이터로는 현재 워커 구성이 성립하지 않는다**는
더 큰 문제가 드러났다. 이 문서는 그 범위에 맞춰 Engine B 를 다시 짠 안이다.

브랜치: `shuu15`

---

## 1. 무슨 일이 있었나

배포에서 나온 질문: **"할 일도 많고 휴가도 신청해야하고 업무도 해야하는데 뭐부터 하는게 좋을까"**

답변: *"현재 한도윤의 휴가는 프로젝트에 큰 영향을 미치지 않으나, 이민주, 박지원, 한도윤의
업무 과부하가 문제로 지적됨."*

셋 다 `app/tools/demo_data.py` 의 픽스처 인물이다. 답변의 모든 문장이 픽스처와 일치했다 —
"한도윤의 휴가"는 `LEAVES` id=2003(8/17~18), "세 명의 과부하"는 `TODOS` 에서 지연 2건씩인
정확히 그 셋이었다.

### 경로

```
프로젝트 컨텍스트 없음 → context.projects 빈 배열 → candidates 빈 배열
→ context_builder.py:208   if "skill_fit" in plan.focus or not candidates:
→ context_builder.py:212   list_department_members.ainvoke({})   ← 필터 없는 전원 조회
→ demo_data 8명 전원이 후보로 유입
```

**후보가 없으면 되묻는 대신 명부를 긁었다** — 이 한 줄이 사고의 핵심이다.

배포 서버의 `MOCK_BACKEND` 는 `false` 로 제대로 설정돼 있었다. `list_department_members` 를
비롯한 인력 조회 함수들이 `uses_fixtures` 검사조차 없이 `demo_data` 로 직행했기 때문에
설정과 무관하게 픽스처가 나갔다.

### 원래 답할 수 있었던 질문이다

저 질문은 **전부 본인 스코프**다. `/me`, `/tasks`(본인 주간), `/schedules`, `/leaves/balance` —
네 개 다 실API 가 있고 개인정보 문제도 없다. 본인 질문을 본인 데이터로 답하는 경로가
없어서 프로젝트 분석으로 끌려갔고, 후보를 못 찾자 명부를 긁은 것이다.

---

## 2. 설계 원칙

### 원칙 1 — 권한 동등성

> **에이전트는 요청자가 화면에서 이미 볼 수 있는 것만 본다.**

"타인 정보 금지"가 아니다. 프로젝트 팀원끼리는 칸반보드에서 서로의 할 일을 보고
캘린더에서 서로의 휴가를 본다. 에이전트가 그걸 읽는 건 권한 상승이 아니다.
문제는 **요청자가 클릭해서 갈 수 없는 곳을 에이전트가 대신 가는 것**이다.

BE 의 허용/차단이 이미 같은 선을 그어놨다.

| 열려 있음 | 막혀 있음 |
|---|---|
| 타인 휴가 **기간** (`/leaves?userIds=`) | 휴가 **사유** (마스킹) |
| 타인 **일정** (`/schedules?userIds=`) | 타인 **잔여연차** (`/leaves/balance` 본인 한정) |
| 프로젝트 내 할 일·담당자·회의록 | 타인의 **다른 프로젝트 이력** (`/projects` 요청자 스코프) |
| | **전사 인력 명부** (`/users` keyword 필수) |

> `AgentUserToolService`: "keyword를 필수로 둔다. 없으면 전사 직원 명부가 통째로 넘어간다."

**"언제 자리에 없는가"는 열고, "그 사람이 어떤 사람인가"는 닫았다.**
우리는 그 선을 픽스처로 넘고 있었다.

### 원칙 2 — 데이터 경계가 분석 경계다

BE 가 주는 것으로 성립하는 축만 남긴다. 없는 데이터를 픽스처로 메우지 않는다.

### 원칙 3 — 못 하는 건 못 한다고 답한다

근거가 없으면 축을 빼고 기록한다. 되물을 수 있으면 되묻는다.
그럴듯한 답보다 "이건 못 봅니다"가 낫다.

---

## 3. 안 쓰고 있던 데이터 셋 — 여기가 제일 컸다

BE 가 이미 주는데 Engine B 가 한 번도 건드리지 않던 데이터가 셋 있었다.
개인 평가와 무관하고, BE 에 아무것도 요청하지 않아도 되고, 분석 품질을 크게 올린다.

| 데이터 | 엔드포인트 | 쓸모 |
|---|---|---|
| **마일스톤** | `/projects/{id}/milestones` | `isOverdue`·`isNext`·완료율. **기간 제약 없이 전체를 받는 유일한 일정 근거** |
| **회의록** | `/projects/{id}/meetings`, `/meetings/{id}` | 내용·후속 조치. "하기로 한 것 대비 진행" 분석 |
| **게시글** | `/projects/{id}/posts` | 프로젝트 내 이슈·공지 (이번 반영에는 미포함) |

진행률을 할 일 DONE 비율로 계산하고 있었는데, `/tasks` 는 주 단위 조회라 그 비율이
부분 집계일 수 있다. 마일스톤이 훨씬 정확한 근거다.

---

## 4. 축(워커) 재편

| 축 | 조치 | 내용 |
|---|---|---|
| `hcm.skill_fit` | **성격 전환** | 점수 매기기 → 역할 매칭. `fit_score` 제거, 순위 제거 |
| `hcm.workload` | **주어 전환** | 사람 → 기간. 상태 라벨(OVERLOADED) 제거, `crunch_periods` 신설 |
| `project.risk` | 근거 축소 | 마일스톤·예산·가용성 기반. 할 일은 컨텍스트 범위 안에서만 |
| `project.priority` | 범위 명시 | 주 단위 제약을 답변에 밝힌다 |
| `project.cost` | 유지 | 유일하게 온전한 축. `committed` 미반영만 명시 |

### `skill_fit` — 폐기가 아니라 축소

처음에는 폐기를 검토했다. 근거 4가지 중 입사일(BE 응답에 필드 없음)과 타인의 과거
프로젝트 이력(요청자 스코프로 차단)을 확보할 수 없고, 무엇보다 `fit_score` 0~100 이
replan 을 타면 실제 업무 재배분 제안이 되기 때문이다. LLM 이 사람에게 점수를 매겨
배치를 바꾸는 구조였다.

다만 **프로젝트 내부로 한정하면 근거가 성립한다.**

- 참여자가 이 프로젝트에서 맡은 역할 → `/projects/{id}/members` 의 `role` ✅
- 참여자가 이 프로젝트에서 처리한 할 일 → 컨텍스트의 할 일 목록 ✅

둘 다 프로젝트 화면에 이미 보이는 정보다. 그래서 **점수를 없애고 근거만 제시하는**
형태로 남겼다. "A가 72점"이 아니라 "A는 이 프로젝트 FE 역할이고 결재선 화면 개편을
처리했다" 라고 답하고, 고르는 건 사용자다.

### `workload` — 주어를 사람에서 기간으로

- 이전: "이민주, 박지원, 한도윤의 업무 과부하"
- 이후: "8월 17~21일에 마감 3건이 몰려 있고, 그 중 2건의 담당자가 휴가로 부재"

같은 데이터로 더 정확하다. 집계 범위가 프로젝트 할 일뿐이라 "그 사람이 바쁘다"는
애초에 단정할 수 없는 진술이기도 했다.

---

## 5. 파이프라인 — Data Gate

```
Router → Context Builder → [Data Gate] → Workers → Validator → Synthesis
                                ↓ 근거 부족
                          축 스킵 + 기록
```

Context Builder 뒤에서 **코드가** 판정한다. 근거가 없는 축은 워커를 띄우지 않는다.

| 축 | 필요한 것 |
|---|---|
| `priority` · `risk` | 대상 프로젝트 |
| `cost` | 예산 정보 |
| `skill_fit` · `workload` | 프로젝트 참여자 |

예전에는 컨텍스트가 비어도 "워커가 도구로 직접 찾아라"고 넘겼고, 그 경로가 전사 명부
조회로 이어졌다. 이제 근거가 없으면 `context.skipped` 에 기록하고 `graph._dispatch_workers`
가 그 축을 건너뛴다. 스킵 목록은 그대로 답변의 "확인하지 못한 것"이 된다.

---

## 6. 반영된 변경

| 파일 | 변경 |
|---|---|
| `app/engine_b/context_builder.py` | 전원 긁기 제거 · Data Gate 신설 · 마일스톤/회의록 적재 · 가용성 계산 이관 |
| `app/tools/hr_tool.py` | 전면 재작성. 픽스처 폴백 제거, 휴가·일정 실API(`userIds` 일괄), 금지 툴 4종 삭제 |
| `app/tools/project_query.py` | `_milestones()` · `_meetings()` 추가 |
| `app/engine_b/graph.py` | Data Gate 결과로 워커 스킵 |
| `app/engine_b/validator.py` | 순위 검사(`NO_ALTERNATIVE`·`SCORE_RANGE`) 제거, 부재 미고지를 warning 으로 |
| `app/workers/hr/skill_fit.py` · `app/prompts/skill_fit.py` | 역할 매칭으로 재작성 |
| `app/workers/hr/workload.py` · `app/prompts/workload.py` | 기간 중심으로 재작성 |
| `app/schemas/state.py` | `MilestoneSnapshot`·`MeetingSnapshot` 추가, `MemberSnapshot.hire_date` 제거, `skipped` 추가 |
| `app/tests/conftest.py` | 픽스처를 API 자리에 끼우는 `fixture_backed_hr` 신설 |
| `app/config.py` · `app/main.py` | `data_source_status()` 신설 → 기동 로그·`/health` 가 데이터 출처를 노출 |
| `app/workers/me/my_week.py` · `app/prompts/my_week.py` | **`me.my_week` 축 신설** |
| `app/prompts/analysis_router.py` | `me` 도메인 추가 + 사고가 난 질문을 첫 few-shot 으로 |

### 기동 로그 · `/health` 에 데이터 출처 노출

이게 안 보여서 배포가 픽스처로 도는 걸 아무도 몰랐다. 이제 둘 다 같은 값을 낸다.

```json
{"mode": "fixtures", "mockBackend": true, "backendBaseUrl": "http://localhost:3001",
 "internalApiKeySet": false, "inboundApiKeySet": false}
```

픽스처 모드이거나 `INTERNAL_API_KEY` 가 비어 있으면 기동 때 경고를 찍는다.
**키 값 자체는 절대 싣지 않는다** — 설정 여부(bool)만 낸다.

### `me.my_week` — 사고가 난 질문의 정답 경로

`/me` · `/tasks`(본인 주간) · `/schedules`(본인) · `/leaves/balance`(본인) 넷만 쓴다.
`userIds` 를 안 실으면 BE 가 `X-Run-Id` 로 역산한 본인으로 스코프를 고정하므로,
**남의 데이터가 섞일 경로가 구조적으로 없다.** 도구도 주지 않았다 — 컨텍스트가 곧 전부고
더 찾아 헤맬 곳이 없어야 한다.

라우터 프롬프트에 "주어가 '나'면 me 하나로 끝낸다"를 못 박고, 실제로 틀렸던 질문을
첫 번째 few-shot 으로 박아뒀다.

### Validator 게이트

재시도로도 `error` 를 못 고친 축은 통합에서 **제외**한다. 예전에는 "남은 위반을 그대로
보고한다"며 근거가 틀린 축까지 답변에 실었다 — 존재하지 않는 할 일을 인용한 위험 분석이
그대로 사용자에게 나갔다. 이제 그 축은 `context.skipped` 로 옮겨 "확인하지 못함"이 된다.

### 픽스처 격리 — 폴백 경로를 없앴다

`demo_data.py` 를 `app/tests/fixtures/` 로 옮기고, 프로덕션 코드에서 픽스처 참조를
전부 제거했다. `budget_tool` · `project_query` 에 남아 있던 조용한 폴백도 같이 없앴다.
`list_pending_approvals` 는 대응 API 가 없어 툴 자체를 삭제했다.

조회 실패는 이제 **빈 결과**다. 값을 지어내지 않고, 호출부가 `missing` 에 남긴다.

백엔드 없이 돌려야 할 때는 픽스처를 분기로 넣는 게 아니라 **내부도구 자리에 끼운다**
(`app/tests/fixtures/stub.py`). 테스트(conftest)와 수동 실행기(`engine_b/demo.py`)가
같이 쓴다. 스텁도 `project.search` 를 요청자 스코프로 제한한다 — 전체를 주면
실제보다 넓은 결과로 테스트가 통과해버린다.

`test_프로덕션_코드는_픽스처를_임포트하지_않는다` 가 `app/{tools,engine_b,workers,api,
orchestrator,common}` 을 훑어 재발을 막는다.

### `UNKNOWN_SUBJECT` 오탐 수정

한 위험이 여러 할 일에 걸리는 게 정상이라 모델은 `subject` 한 칸에
`"todo:101, todo:102, todo:106"` 처럼 몰아 적는다. 검증기가 앞 5글자만 잘라 비교해서
**실재하는 할 일을 "없다"고 잡고 있었다.** 정규식으로 참조를 전부 뽑아 각각 대조하도록
고쳤고, `RiskItem.subject` 도 `subjects: list[str]` 로 바꿨다.

### 삭제한 툴

픽스처를 실데이터로 바꾼 게 아니라 **기능을 접었다.**

| 툴 | 사유 |
|---|---|
| `list_department_members` | 전사/부서 명부. BE 가 의도적으로 막은 경로 |
| `get_user_project_history` | 타인 경력 정보 |
| `get_leave_balance` | 타인 근태 정보 |
| `list_user_tasks` | 프로젝트 밖 타인 할 일 |

### 회귀 테스트

- `test_후보군은_프로젝트_참여자를_넘지_않는다` — `focus=["skill_fit"]` 로 예전 전사 조회 조건을
  재현해도 후보가 참여자를 넘지 않는지 확인. **이 사고의 방어선**
- `test_참여자_밖_id는_조회하지_않고_기록만_남긴다`
- `test_데이터게이트가_근거없는_축을_건너뛴다`
- `test_후보군_밖의_사람을_제시하면_잡는다` — 마지막 방어선(Validator)

---

## 7. BE 요청 (4건)

권한 동등성 기준으로 다시 추렸다. 처음에 5건을 생각했다가 보안 판단으로 2건까지 줄였는데,
기준을 정확히 잡으니 4건이 맞다.

| 우선순위 | 요청 | 근거 |
|---|---|---|
| **1** | **집계 엔드포인트** `GET /projects/{id}/workload-summary?from=&to=` → 멤버별 `{열린 할 일 수, 지연 수, 기간 내 마감 수, 부재일수}` | **원자료 대신 집계값**이라 노출 면적이 줄고, BE 가 계산하니 LLM 이 세다 틀릴 일이 없고(현재 `UNKNOWN_SUBJECT` 위반의 원인), 응답도 작다. 프라이버시·정확도·성능을 동시에 잡는다 |
| **2** | 기간 기반 프로젝트 할 일 `GET /projects/{id}/tasks?from=&to=` | 현재 주 단위(`weekOffset`, ±8주)라 전체 기간을 못 본다. 칸반보드에 이미 보이는 정보라 권한 상승이 아니다. 성능 제약으로 보인다 |
| **3** | 결재 대기 금액 조회 | `committed` 를 못 받아 잔액이 실제보다 크게 보인다. 프로젝트 예산이지 개인정보가 아니다 |
| **4** | 프로젝트 내 완료 이력 `GET /projects/{id}/tasks?assigneeId=&status=DONE` | "이 프로젝트에서 이 사람이 뭘 끝냈나". 칸반보드에 보이는 정보. `skill_fit` 근거가 단단해진다 |

### 요청하지 않는 것

요청자가 화면으로 갈 수 없는 곳이다.

- 전사·부서 인력 명부
- 타인의 **다른** 프로젝트 이력
- 타인 잔여연차
- 휴가 사유
- `hireDate` 필드 — 근속 기반 역량 추정 자체를 접었다

---

## 8. 남은 작업

| | 내용 |
|---|---|
| **게시글 컨텍스트** | `/projects/{id}/posts` 미반영 |
| **`AnalysisContext.workloads` 개명** | `availabilities` 로. 내부 필드명이라 기능에는 영향 없어 후순위 |
| **`Domain.me` 하위 호환 확인** | `request.py`·`response.py` 가 공유하는 어휘라 담당자 1·3 쪽 처리 확인 필요 |

### 검증

| 스위트 | 결과 |
|---|---|
| `test_workers` | 57 통과 |
| `test_backend_mapping` · `test_vacation_worker` | 15 통과 |
| `test_internal_api_auth` · `test_engine_b_history_wiring` · `test_mail_domain_registered` | 16 통과 |
| 도구 배터리 (`app.tests.test_tools`) | 전부 통과 |

LLM 을 태우는 무거운 스위트(`test_engine_b` 등)는 돌리지 않았다.

---

## 9. 확인 사항

- **인사 데이터 취급 기준** — 이 문서의 보안 판단은 코드만 보고 내렸다. 사내 기준이 따로 있으면
  그쪽이 우선이다. 특히 툴 4종 삭제는 되돌리기 번거로우니 확인이 필요하다
- **배포 `.env`** — `MOCK_BACKEND=false` 는 확인됐다. `INTERNAL_API_KEY` 가 채워져 있는지는 미확인.
  비어 있으면 BE 호출이 401 → 이제는 빈 결과 + 경고 로그로 드러난다
- **담당자 경계** — `app/config.py`, `app/main.py` 는 담당자 1 파일이다.
  mock 상태를 기동 로그·`/health` 에 노출하는 건은 별도 요청 대상
