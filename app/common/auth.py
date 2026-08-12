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

★ 수신 키를 송신 키와 분리해 둔 이유 (settings.inbound_api_key)
  나가는 호출(FastAPI→Spring)은 settings.internal_api_key 를 헤더에 실어 보내야
  하고 BE 가 이걸 요구한다. 한 값으로 둘을 겸하면 그 키를 채우는 순간 수신 검증도
  같이 켜지는데, 규격상 Spring→FastAPI 방향엔 인증 헤더가 없어서 BE 의 모든 호출이
  401 이 된다. 한쪽만 켤 수 없는 구조라 값을 둘로 나눴다.

★ ①을 지금 "느슨한 모드"로 운영하기로 확정한 이유 (2026-08-12 결정)
  Spring→FastAPI 인증 헤더 없이 이미 배포가 나갔고, 앞으로도 이 헤더 없이 가기로
  정했다. 대신 네트워크 경계로 막는다 — docker-compose.yml 이 이 서버를
  "127.0.0.1:3002:3002"로만 호스트에 노출한다(컨테이너 안은 0.0.0.0 이지만,
  호스트 루프백 밖에서는 애초에 이 포트에 닿을 수 없다). Spring 이 이 서버와
  같은 호스트에서 루프백으로 호출하는 구조라면, "인증 안 된 아무나 호출" 이라는
  시나리오 자체가 네트워크 층에서 막힌다 — 그래서 애플리케이션 층 인증
  (inbound_api_key)을 추가로 요구하지 않기로 했다.

  ⚠️ 이 결정은 "Spring이 이 서버와 같은 호스트에서 루프백으로 접근한다"는 전제에
  묶여 있다. 나중에 배포 구조가 바뀌어 Spring 이 별도 컨테이너에서 도커 내부
  네트워크(호스트 루프백이 아닌 경로)로 접근하게 되면 이 전제가 깨진다 — 그때는
  이 파일을 다시 검토해야 한다. inbound_api_key 를 채우면 verify_internal_api_key()
  는 그대로 검증을 시작하도록 짜여 있으니, 코드를 더 고칠 필요 없이 .env 값만
  채우고 BE 와 헤더 전송을 맞추면 된다.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger("common.auth")
_startup_logged = False   # 매 요청이 아니라 프로세스당 한 번만 상태를 알린다


async def verify_internal_api_key(
    x_internal_api_key: str | None = Header(default=None),
) -> None:
    """라우터/앱 include_router 에 `dependencies=[Depends(verify_internal_api_key)]`
    로 건다 — 개별 엔드포인트마다 챙길 필요 없이 그 라우터(및 중첩된 하위
    라우터) 전체에 적용된다(app/main.py 참고).

    BE가 보내는 X-Internal-Api-Key 헤더가 settings.inbound_api_key 와 정확히
    일치해야 통과한다. 실패하면 401 — 어떤 값이 왜 틀렸는지는 응답에 담지
    않는다(공격자에게 힌트를 주지 않기 위해서다).

    inbound_api_key 가 비어 있으면(현재 운영 중인 기본 상태) 검증을 건너뛴다 —
    이 경우 네트워크 경계(docker-compose 의 127.0.0.1 바인딩, 모듈 docstring
    참고)가 유일한 방어선이라는 뜻이다. 요청마다 로그를 남기면 소음만 늘어서,
    프로세스 시작 후 첫 요청 때 한 번만 상태를 알린다.
    """
    global _startup_logged
    settings = get_settings()

    if not settings.inbound_api_key:
        if not _startup_logged:
            log.info(
                "INBOUND_API_KEY 미설정 — 애플리케이션 인증 없이 요청을 통과시킨다. "
                "네트워크 경계(127.0.0.1 바인딩)로만 막는 구조로 운영 중 "
                "(app/common/auth.py 모듈 docstring 참고)"
            )
            _startup_logged = True
        return

    if not hmac.compare_digest(x_internal_api_key or "", settings.inbound_api_key):
        raise HTTPException(status_code=401, detail="unauthorized")
