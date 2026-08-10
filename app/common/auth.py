# app/common/auth.py
"""인증 공통 인프라. (담당자1)

이 파일은 두 가지 다른 층위의 "인증"을 담는다 — 헷갈리지 않게 구분해 둔다.

① 서비스 간 인증 (이 파일에 구현됨) — "이 HTTP 요청이 진짜 우리 BE 에서 왔는가"
  이 Agent 프로세스는 로그인/세션 인증을 직접 하지 않는다 — 그건 BE(스프링)
  책임이고, BE가 이미 인증된 사용자에 대해 run_id 를 발급해 이 서버를 내부적으로
  호출하는 구조다(app/api/agent.py 의 RunRequest.runId 주석: "BE 가 발급").
  즉 이 서버가 확인해야 할 건 "사용자가 누구인지"가 아니라 "이 요청이 진짜 BE
  에서 온 게 맞는지" 뿐이다 — run_id 자체는 비밀값이 아닌 단순 식별자라 URL/
  바디 어디에 있어도 도청·추측만으로 위조될 수 있고, 그것만으로는 호출자를
  증명하지 못한다. 지금까지는 이걸 확인하는 코드가 어디에도 없어서(app/main.py
  에 CORS 미들웨어만 있었다) 이 서버 주소만 알면 누구든 아무 runId 로
  /api/agent/runs 등을 직접 호출할 수 있었다 — verify_internal_api_key() 가
  그 구멍을 막는다.

② 권한 상속(Auth Context) — 아직 미구현, 별도 작업
  요청자의 토큰·권한을 Tool 호출까지 그대로 전달해, 에이전트가 사용자 본인보다
  더 많은 데이터를 못 보게 막는 것. ①이 "누가 이 서버를 부르는가"를 막는
  경계라면, ②는 그 안에서 "이 사용자가 원래 볼 수 있었던 것만 보는가"를 막는
  경계라 서로 다른 문제다. 지금 조회/쓰기 도구들은 X-Run-Id 로 BE 가 권한을
  역산하는 구조(app/tools/registry.py)라 BE 내부 API 쪽에서 이미 어느 정도
  커버되지만, 에이전트가 여러 run/도메인을 넘나드는 경로가 늘어나면 이 파일에
  별도로 채워야 할 수 있다.

★ ①이 아직 "느슨한 모드"로 시작하는 이유
  settings.internal_api_key(X-Internal-Api-Key)는 BE 팀과 발급/배포 방식을
  아직 합의 전이라 값이 비어 있다(app/config.py: "아직 미수령") — run_id→user_id
  API 와 같은 처지다. 값이 비어 있는 동안은 검증을 건너뛴다(그래야 로컬/개발
  환경, 지금까지의 테스트가 안 막힌다) — 대신 매 요청마다 경고 로그를 남겨
  "지금 이 배포는 인증이 꺼져 있다"는 사실이 조용히 묻히지 않게 한다. BE 키가
  나오는 대로 .env 에 INTERNAL_API_KEY 만 채우면 아래 검증이 코드 변경 없이
  즉시 강제 모드로 바뀐다.
"""

from __future__ import annotations

from fastapi import Header, HTTPException

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger("common.auth")


async def verify_internal_api_key(
    x_internal_api_key: str | None = Header(default=None),
) -> None:
    """라우터/앱 include_router 에 `dependencies=[Depends(verify_internal_api_key)]`
    로 건다 — 개별 엔드포인트마다 챙길 필요 없이 그 라우터(및 중첩된 하위
    라우터) 전체에 적용된다(app/main.py 참고).

    BE가 보내는 X-Internal-Api-Key 헤더가 settings.internal_api_key 와 정확히
    일치해야 통과한다. 실패하면 401 — 어떤 값이 왜 틀렸는지는 응답에 담지
    않는다(공격자에게 힌트를 주지 않기 위해서다).
    """
    settings = get_settings()

    if not settings.internal_api_key:
        log.warning(
            "INTERNAL_API_KEY 미설정 — 인증 없이 요청을 통과시킴 "
            "(BE 키 발급 전 임시 상태, 프로덕션 배포 전 반드시 채울 것)"
        )
        return

    if x_internal_api_key != settings.internal_api_key:
        raise HTTPException(status_code=401, detail="unauthorized")
