# app/engine_b/entry.py
"""
엔진 B 진입점 — 이 요청이 replan(3안 생성 HITL)인지 일반 분석(단발)인지를 가른다.

api/agent.py 의 start_run() 이 route=="engine_b" 로 분류된 요청마다 한 번 부른다.
analysis_router.route() 의 LLM 1콜로 domain/focus/mode 를 정하는데, 여기서는 그 중
mode 만 본다:
  mode == replan  → engine_b.replan_agent (3안 제시 + 엔진A 와 동일한 HITL 승인)
  그 외(analysis) → engine_b.runner.run_engine_b (단발 분석, api/agent.py 의
                    _engine_b_stream 이 그대로 부른다)

★ 예전엔 이 판단 자체가 없었다 — start_run() 이 route=="engine_b" 면 무조건
  run_engine_b 로 보내서, replan 3안 생성/HITL 경로(예전 replan_service.py +
  suggestion_store.py)가 실제 요청 흐름 어디에도 안 붙어 있었다. 지금은 이 파일이
  그 갈림길 역할을 한다.
"""
from __future__ import annotations

from app.engine_b.analysis_router import route
from app.schemas.state import AnalysisPlan, AnalysisRequest, UIContext


async def route_engine_b(req) -> AnalysisPlan:
    """요청 1건의 처리 모드(analysis/replan/derivation)를 정한다."""
    return await route(_to_request(req))


def _to_request(req) -> AnalysisRequest:
    """RunRequest → Engine B 라우팅 입력.

    ★ user_id: 내부 규격상 요청 바디엔 userId 가 없다(X-Run-Id 로 BE 가 역산).
      여기선 라우팅(도메인/모드 판단)에만 쓰이고 실제 데이터 조회엔 안 쓰이므로
      화면 폼값이 있으면 쓰고 없으면 0으로 둔다(권한 판정은 어차피 BE 몫).
    """
    fs = req.screenContext.formState or {}
    return AnalysisRequest(
        query=req.goal,
        user_id=int(fs.get("userId") or 0),
        ui_context=UIContext(
            screen=req.screenContext.screen,
            project_id=fs.get("projectId"),
        ),
    )
