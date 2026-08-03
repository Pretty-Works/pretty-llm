# pretty-llm 개발 로드맵 (담당자 1 기준)

> 계정/환경을 옮겨도 이 문서 하나로 이어서 작업. 마지막 갱신: walking skeleton + HITL 미들웨어 동작 확인 시점.

---

## 0. 프로젝트 한 줄

PM을 위한 멀티에이전트 그룹웨어. 여러 LLM 에이전트가 일정·예산·리스크·인력을 병렬 분석해 PM 의사결정을 돕는다. 에이전트가 **제안**하고 사람이 **승인(HITL)**하는 구조.

- 기술: FastAPI + 오케스트레이션, 라우팅 + LangGraph(create_agent, 미들웨어) + Pydantic + RAG + Docker
- 원격: github.com/Pretty-Works/pretty-llm  / 현재 브랜치: `park1`
- 실행: `uv run uvicorn app.main:app --reload` → http://127.0.0.1:8000/docs

### 담당 분담
- **담당자 1 (나)**: orchestrator, common(hitl·auth·llm_client·exceptions), engine_a
- 담당자 2: engine_b 코어(analysis_router·context_builder·validator·synthesis), project·hr 워커
- 담당자 3: engine_b 재계획(scenario·tradeoff), meeting 워커

---

## 1. 전체 개발 로드맵 (5단계)
# 점검해보고 수정해야할 부분이나 디벨롭시킬부분있는지 확인할것
| 단계 | 내용 | 상태 |
| --- | --- | --- |
| 1 | 계약(schemas) 확정 — 팀 합의 | ✅ 코드 완료(팀 리뷰 필요) |
| 2 | walking skeleton — 기능 하나 끝까지 관통 | ✅ 연차 승인 완료 |
| 3 | 패턴 복제 — 도메인 확장 | 🔶 project 1개 복제됨 |
| 4 | mock→실제 교체 + 안전망(exceptions) | ⬜ |
| 5 | 배포(Docker) + 성능평가(eval) | ⬜ |

---

## 2. 지금까지 한 것 (완료)

### 공통 기반
- `schemas/state.py` — 내부 타입: AuthContext(user_id만, role 제거), WorkerOutput(`{dimension,result,reasoning,confidence}`), RouteDecision, Violation, EngineBState
- `schemas/request.py` — AgentRequest(user_id 포함), Staffing/Replan/DecisionRequest
- `schemas/response.py` — ApiResponse(`{errorCode,message,result}` + ok/fail), AiSummary, SuggestionResponse
- `config.py` — load_dotenv + OpenAI(gpt-4o-mini) 기본, 키는 .env의 OPENAI_API_KEY
- `main.py` — FastAPI 앱, /health, routes 연결

### 연차 승인 (교재 방식 HITL) — 실제 LLM으로 동작 확인 ✅
- `engine_a/vacation_agent.py` — @tool(check_vacation_impact 읽기 / approve_vacation 쓰기) + create_agent + **HumanInTheLoopMiddleware**(approve_vacation만 interrupt) + InMemorySaver
- `common/hitl.py` — start()/resume() = invoke → `__interrupt__` 확인 → Command(resume). thread_id로 두 요청 이어붙임
- `api/vacation.py` — POST /approve(승인대기 반환), POST /{thread_id}/decision(재개)
- **검증**: 요청 → needs_approval:true + thread_id → decision approve → "승인되었습니다" 까지 200 OK 확인

### 프로젝트 분석 (엔진 B 직접 진입) — mock
- `engine_b/analysis_router.py` — 얇은 진입(워커 병렬 mock 호출)
- `engine_a/engine_b_client.py` — **WORKER_SETS 매핑**(도메인별 워커 세트) + 고정 mock. vacation→risk‖workload, project→priority‖risk‖cost
- `api/project.py` — POST /projects/analyze
- `orchestrator.py` — HANDLERS 매핑(project). vacation은 에이전트가 직접 처리하므로 여기 없음

---

## 3. 설계 원칙 (코드에 박아둔 것 — 계속 지킬 것)

1. **인터페이스 고정, 속만 교체** — `engine_b_client.analyze()`가 mock↔실제 교체 지점. 반환형(list[WorkerOutput]) 유지하면 부르는 쪽 안 바뀜
2. **분기는 if가 아니라 매핑(dict)** — orchestrator HANDLERS, routes include_router, WORKER_SETS. 도메인 추가 = 표에 한 줄
3. **단일책임** — 함수 잘게 쪼갬(교체가 국소적)
4. **HITL은 교재 방식** — LangGraph interrupt/미들웨어/checkpointer 사용(하네스 배점 대응). 수동 dict 재발명 금지
5. **참고 우선** — 코드 짤 때 `~/dev/study/llm-hankyung`의 v0/v1 교재 방식을 먼저 참고. 특히 v1 09_Multi_Agent, 06_Middleware(HITL), 04_Advanced(Branching-Parallel), 12_Testing(eval)

---

## 4. 앞으로 할 것

### 4단계 직전 — 정리/커밋 (지금 바로)
- [ ] **옛 dict 방식 잔재 삭제**: `engine_a/optimizer.py`, `engine_a/recommendation.py` (이제 안 쓰임)
- [ ] park1 커밋 & push, 팀에 schemas·HITL 패턴 공유(PR)

### 3단계 마저 — 패턴 복제 (선택)
- [ ] orchestrator에 LLM 기반 domain 분류 붙이기 (지금은 domain_hint)
- [ ] meeting / hcm 도메인 복제 (같은 패턴, WORKER_SETS에 한 줄)

### 4단계 — mock→실제 + 안전망
- [ ] **담당자 2·3의 실제 엔진 B 완성 후** `engine_b_client.analyze()` 속을 진짜 호출로 교체(한 줄)
- [ ] `approve_vacation` 안을 실제 백엔드 호출로 교체(지금 텍스트만)
- [ ] `common/exceptions.py` 채우고 main에 register — 503(LLM타임아웃)/422(데이터부족)/429(재시도초과). **지금은 불필요, 이 단계에서**
- [ ] `common/auth.py` — user_id를 Tool 호출까지 전달(권한 상속)
- [ ] `tools/suggestion_tool.py` — 제안·감사로그 백엔드 저장 (백엔드와 규격 합의 필요)
- [ ] checkpointer InMemorySaver → 영구 저장(선택)
- [ ] 날짜 버그: LLM이 "3월 첫째주"를 2024로 해석 → "오늘 날짜" 컨텍스트 주입

### 5단계 — 배포 + 평가
- [ ] Dockerfile / docker-compose (배점 20)
- [ ] `tests/eval/` — openevals/agentevals로 성능평가 (배점 10). 교재 12_Testing 참고

---

## 5. 백엔드 팀과 확인할 것 (블로킹 요소)
- 최종 API 명세서에 `/api/internal/v1/...` (AI↔백엔드) 규격 존재. 인증은 백엔드가 검증 후 **user_id를 body로 전달**(검증 불필요 확정)
- 아직 없는 워커용 조회 API 2개(요청 필요): 사원 스킬·등급(`skills[]{name,grade}`), 사원 워크로드
- 응답 래퍼 통일: 로그인 API는 `errorCode:"SUCCESS"`, 우리는 `errorCode:null` → 통일 필요
- 제안(suggestion)·감사로그를 백엔드가 저장하는지 AI가 임시보관하는지

---

## 6. 현재 파일 상태 (핵심만)

```
app/
├── main.py                 ✅ 서버 기동 + routes
├── config.py               ✅ OpenAI + load_dotenv
├── schemas/                ✅ state·request·response
├── orchestrator/
│   └── orchestrator.py     ✅ HANDLERS(project). vacation은 제외
├── engine_a/
│   ├── vacation_agent.py   ✅ create_agent + HITL 미들웨어 (동작확인)
│   ├── engine_b_client.py  ✅ mock (WORKER_SETS 매핑)
│   ├── optimizer.py        ❌ 삭제 예정 (dict 잔재)
│   └── recommendation.py   ❌ 삭제 예정 (dict 잔재)
├── engine_b/
│   └── analysis_router.py  🔶 얇은 진입 (담당자2가 실제 그래프로)
├── common/
│   ├── hitl.py             ✅ interrupt/resume 헬퍼
│   ├── auth.py             ⬜ 주석만
│   ├── llm_client.py       ⬜ 주석만
│   ├── exceptions.py       🔶 뼈대만(TODO) — 4단계에 채움
│   └── ...
├── api/                    ✅ vacation·project·routes
├── tools/suggestion_tool.py⬜ 주석만
└── tests/                  ⬜
```

