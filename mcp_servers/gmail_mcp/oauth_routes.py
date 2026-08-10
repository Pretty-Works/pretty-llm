# mcp_servers/gmail_mcp/oauth_routes.py
"""OAuth HTTP 엔드포인트. 다이어그램 ②~⑦ 전체가 이 파일 안에서 끝난다.

    ② "Gmail 연결하기" 클릭 (Agent Chat 안, 즉 run_id가 이미 있는 상태)
         → 메인 백엔드(Spring/Agent)가 POST /internal/gmail/connect-url 호출
           (서버 투 서버, X-Internal-Api-Key)해서 Google 동의화면 URL을 받아
           프론트에 그대로 돌려준다. 이 시점까지 Agent/메인백엔드는 code도 token도 못 본다.
    ③ 프론트가 그 URL로 브라우저 리다이렉트 → Google 동의화면
    ④ 사용자 승인
    ⑤ Google → 이 서버의 /oauth/gmail/callback 으로 code, state 전달
    ⑥ 이 서버가 code를 access/refresh token으로 교환
    ⑦ credential_store에 암호화 저장(user_id↔구글계정 1:1 위반이면 거부) → 프론트 성공/실패 페이지로 리다이렉트

★ user_id를 어디서도 직접 안 받는다 — 전부 run_id를 받아서 run_resolver.py로
  user_id를 역산한다(⑧ 설계: Gmail 연결은 Agent Chat 안에서만 발생하므로 run_id가
  항상 존재한다는 전제). 예전에는 이 엔드포인트들이 user_id를 그대로 받았는데,
  그러면 호출하는 쪽(Agent의 app/api/integrations.py 스텁 등)이 "지금 진짜 로그인한
  사용자가 누구인지"를 스스로 증명할 방법이 없어 아무 user_id나 넣어 남의 계정에
  연결시킬 수 있는 구멍이 있었다. run_id는 BE가 발급하고 BE만 user_id로 되돌릴 수
  있는 값이라 이 구멍이 막힌다.

★ state 서명은 여전히 필요하다 — run_id→user_id 해석은 connect-url 발급 시점(②) 에
  "한 번만" 하고, 그 결과 user_id를 예전과 똑같이 state_token으로 서명해서 Google에
  보낸다. 콜백(⑤~⑥)은 그 서명만 검증하면 되고 BE를 다시 안 불러도 된다 — CSRF 방지용
  서명+TTL 은 그대로 유지된다.

★ 로그 주의: 성공/실패 모두 결국 localhost:3000(프론트) 으로 리다이렉트한다. 프론트가
  아직 없으면 브라우저는 두 경우 다 ERR_CONNECTION_REFUSED 를 띄운다 — 그래서 브라우저
  화면만 보고는 성공/실패를 구분할 수 없다. 반드시 이 서버의 터미널 로그로 확인한다.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from mcp_servers.gmail_mcp import credential_store, google_oauth, run_resolver, state_token
from mcp_servers.gmail_mcp.config import get_settings
from mcp_servers.gmail_mcp.logger import get_logger

router = APIRouter(tags=["gmail-oauth"])
log = get_logger("oauth_routes")


def _check_internal_key(x_internal_api_key: str | None) -> None:
    """메인 백엔드만 이 서버를 호출할 수 있게 막는다. app 쪽 X-Internal-Api-Key 관례를 그대로 따른다."""
    expected = get_settings().internal_api_key
    if not expected or x_internal_api_key != expected:
        raise HTTPException(status_code=401, detail="X-Internal-Api-Key 불일치")


async def _resolve_or_502(run_id: str) -> str:
    """run_id→user_id. 실패하면 502로 죽인다 — 이 값 없이는 아무것도 못 하므로 조용히
    넘어가면 안 된다(BE의 run_id→user_id API가 아직 없다면 GMAIL_MCP_DEV_RUN_PASSTHROUGH
    로 로컬에서 우회할 것, run_resolver.py 참고)."""
    try:
        return await run_resolver.resolve_user_id(run_id)
    except run_resolver.RunResolutionError as exc:
        log.warning("run_id→user_id 조회 실패: %s", exc)
        raise HTTPException(status_code=502, detail=f"run_id 조회 실패: {exc}") from exc


class ConnectUrlRequest(BaseModel):
    run_id: str  # Agent Chat의 현재 run_id. user_id는 여기서 직접 안 받는다(위 설명 참고).


class ConnectUrlResponse(BaseModel):
    authorize_url: str


@router.post("/internal/gmail/connect-url", response_model=ConnectUrlResponse)
async def create_connect_url(
    body: ConnectUrlRequest,
    x_internal_api_key: str | None = Header(default=None),
) -> ConnectUrlResponse:
    """① Agent Chat에서 '연결하기' 버튼을 누른 시점의 run_id를 받아 URL을 발급한다.

    메인 백엔드는 이 응답을 그대로 프론트에 전달하기만 하면 된다 — code/token 처리 없음.
    """
    _check_internal_key(x_internal_api_key)
    user_id = await _resolve_or_502(body.run_id)
    state = state_token.issue(user_id)
    url = google_oauth.build_authorize_url(state)
    log.info("connect-url 발급 run_id=%s → user_id=%s", body.run_id, user_id)
    return ConnectUrlResponse(authorize_url=url)


@router.get("/oauth/gmail/callback")
async def oauth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    settings = get_settings()

    if error:
        # 사용자가 동의 화면에서 '취소'를 누른 경우 등
        log.warning("콜백 실패: Google이 error=%s 반환 (사용자가 취소했을 가능성)", error)
        return RedirectResponse(f"{settings.frontend_failure_redirect}&reason={error}")

    if not code or not state:
        log.warning("콜백 실패: code 또는 state 누락 (code=%s, state=%s)", bool(code), bool(state))
        return RedirectResponse(f"{settings.frontend_failure_redirect}&reason=missing_code_or_state")

    try:
        user_id = state_token.verify(state)
    except state_token.InvalidState as exc:
        log.warning("콜백 실패: state 검증 실패 — %s", exc)
        return RedirectResponse(f"{settings.frontend_failure_redirect}&reason=invalid_state")

    try:
        token_data = await google_oauth.exchange_code_for_tokens(code)
    except google_oauth.OAuthExchangeError as exc:
        log.warning("콜백 실패: code→token 교환 실패 — %s", exc)
        return RedirectResponse(f"{settings.frontend_failure_redirect}&reason=exchange_failed")

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        # prompt=consent 를 안 넣었거나 이미 한 번 승인한 계정에서 잘 발생한다.
        # refresh_token 없이는 장기 연결이 안 되므로 재동의를 유도한다.
        log.warning(
            "콜백 실패: refresh_token 없음 (user_id=%s). "
            "Google 계정 설정 > 보안 > 타사 앱 액세스에서 이 앱 연결을 해제하고 다시 시도해보세요.",
            user_id,
        )
        return RedirectResponse(f"{settings.frontend_failure_redirect}&reason=no_refresh_token")

    access_token = token_data["access_token"]
    expiry = google_oauth.expiry_epoch(token_data.get("expires_in", 3600))

    try:
        identity = await google_oauth.fetch_google_identity(access_token)
    except Exception as exc:  # noqa: BLE001 — userinfo 조회는 부가 정보라 실패해도 연결 자체는 계속 진행
        log.warning("userinfo 조회 실패(연결은 계속 진행) — %s", exc)
        identity = {"sub": "", "email": ""}

    try:
        await credential_store.save(
            credential_store.GmailCredential(
                user_id=user_id,
                google_subject_id=identity.get("sub", ""),
                google_email=identity.get("email", ""),
                access_token=access_token,
                refresh_token=refresh_token,
                access_expiry=expiry,
                scopes=token_data.get("scope", settings.gmail_scopes),
            )
        )
    except credential_store.AlreadyLinkedError as exc:
        # user_id ↔ 구글 계정 1:1 위반 — 이 구글 계정이 이미 다른 user_id 에 연결돼 있다.
        # 조용히 덮어쓰지 않고 실패로 안내한다(연결된 다른 user_id 값 자체는 응답/프론트에 노출 안 함).
        log.warning(
            "콜백 실패: 이미 다른 user_id에 연결된 구글 계정 — user_id=%s email=%s: %s",
            user_id, identity.get("email", ""), exc,
        )
        return RedirectResponse(f"{settings.frontend_failure_redirect}&reason=google_account_already_linked")

    log.info(
        "콜백 성공: user_id=%s google_email=%s google_subject_id=%s 연결 저장 완료",
        user_id, identity.get("email", ""), identity.get("sub", ""),
    )

    return RedirectResponse(settings.frontend_success_redirect)


@router.get("/internal/gmail/status/{run_id}")
async def connection_status(
    run_id: str,
    x_internal_api_key: str | None = Header(default=None),
) -> dict:
    """메인 백엔드가 '연결됨/안됨' 뱃지를 그리기 위해 조회. 토큰 값 자체는 절대 응답에 안 담는다.

    user_id가 아니라 run_id를 받아서 내부에서 역산한다 — connect-url과 동일한 이유.
    """
    _check_internal_key(x_internal_api_key)
    user_id = await _resolve_or_502(run_id)
    cred = await credential_store.get(user_id)
    if cred is None:
        return {"connected": False}
    return {"connected": True, "googleEmail": cred.google_email}


@router.delete("/internal/gmail/connection/{run_id}")
async def disconnect(
    run_id: str,
    x_internal_api_key: str | None = Header(default=None),
) -> dict:
    """'연결 해제'. 저장된 자격증명을 지운다. (선택: Google 쪽 revoke 엔드포인트도 같이 호출 가능)

    user_id가 아니라 run_id를 받아서 내부에서 역산한다 — connect-url과 동일한 이유.
    """
    _check_internal_key(x_internal_api_key)
    user_id = await _resolve_or_502(run_id)
    await credential_store.delete(user_id)
    return {"connected": False}
