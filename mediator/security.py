"""공개 서버용 보안 계층: 기본 거부(default-deny) 관문, 보안 헤더, 오류 정제, 최소 감사 기록.

관문 규칙
  - 참가자가 이메일 링크로 여는 경로(/approve/, /change/, /materials/)와 정적 자산(/assets/), 화면 뼈대(/),
    로그인(/api/login), 세션 확인(/api/me), 최소 health 만 공개다. 그 밖의 모든 경로는 관리자만 쓴다.
    새 경로를 추가해도 여기에 적지 않으면 자동으로 관리자 전용이 된다.
  - 관리자 전용 경로의 상태 변경 요청은 CSRF 검사(로그인 세션은 토큰, 그리고 Origin)를 통과해야 한다.
  - 참가자 링크 경로에는 접속자(IP)별 속도 제한이 있다.
  - 응답에는 보안 헤더(CSP, 프레임 금지, nosniff, no-referrer ...)가 붙는다. 스택트레이스나 경로는 내보내지 않는다.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse

from common import config
from mediator import auth, store

log = logging.getLogger("fairmeet.security")

PUBLIC_PREFIXES = ("/approve/", "/change/", "/materials/", "/assets/")
PUBLIC_EXACT = frozenset({"/", "/health", "/api/me", "/api/login", "/favicon.ico", "/slack/events"})
LINK_PREFIXES = ("/approve/", "/change/", "/materials/")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; "
       "frame-ancestors 'none'")

GENERIC_ERROR = "일시적인 문제가 생겼어요. 잠시 후 다시 시도해 주세요."


# ---- 감사 기록 --------------------------------------------------------------------------------

def _text_hash(text: str) -> str:
    key = config.GROUP_SECRET.encode("utf-8")
    return hmac.new(key, b"fairmeet/audit/v1" + text.encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def audit(kind: str, *, category: str = "", actor: str = "", route: str = "",
          text: str | None = None) -> None:
    """보안 사건을 최소한으로 남긴다. 입력 원문은 저장하지 않고 길이와 짧은 해시만 남긴다."""
    try:
        with store.connect() as c:
            c.execute("INSERT INTO security_events (at, kind, category, actor, route, text_len, "
                      "text_hash) VALUES (?,?,?,?,?,?,?)",
                      (store.now(), kind, category[:40], actor[:60], route[:80],
                       len(text) if text is not None else None,
                       _text_hash(text) if text else None))
    except Exception:                                            # noqa: BLE001
        log.warning("보안 감사 기록을 남기지 못했습니다 (%s)", kind)


def purge_audit(days: int | None = None) -> int:
    import datetime as dt
    cutoff = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=days or config.SECURITY_LOG_DAYS)).isoformat()
    with store.connect() as c:
        return c.execute("DELETE FROM security_events WHERE at < ?", (cutoff,)).rowcount


# ---- 관문 -------------------------------------------------------------------------------------

def is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


def _json(status: int, detail: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail}, headers=headers)


def _too_many_page() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>FairMeet</title><link rel=stylesheet href=/assets/fairmeet.css>"
        "<body class=participant><main class=wrap><h1>잠시 후 다시 시도해 주세요</h1>"
        "<p class=note>짧은 시간에 요청이 너무 많았어요.</p></main></body>",
        status_code=429, headers={"Retry-After": "60"})


async def _gate(request: Request, call_next):
    path = request.url.path
    if path.startswith(LINK_PREFIXES):
        if not auth.PUBLIC_LINKS.allow(auth.client_ip(request)):
            audit("rate_limited", category="link", route=path.split("/")[1])
            return _too_many_page()
        return await call_next(request)
    if is_public(path):
        return await call_next(request)

    principal = auth.authenticate(request)
    if principal is None:
        audit("forbidden", route=path[:60])
        return _json(401, "로그인이 필요해요.")
    if request.method not in SAFE_METHODS and not auth.csrf_ok(request, principal):
        audit("csrf", actor=principal.key, route=path[:60])
        return _json(403, "요청을 확인하지 못했어요. 화면을 새로고침한 뒤 다시 시도해 주세요.")
    request.state.principal = principal
    return await call_next(request)


async def _headers(request: Request, call_next):
    resp = await call_next(request)
    h = resp.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "no-referrer")
    h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    h.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()")
    if (h.get("content-type") or "").startswith("text/html"):
        h.setdefault("Content-Security-Policy", CSP)
    if not request.url.path.startswith("/assets/"):
        h.setdefault("Cache-Control", "no-store")
    if auth.is_secure_request(request):
        h.setdefault("Strict-Transport-Security", "max-age=15552000")
    return resp


def principal_of(request: Request) -> auth.Principal:
    """관문을 통과한 요청의 주체. 관문 밖에서 호출하면 인증되지 않은 것으로 본다."""
    p = getattr(request.state, "principal", None)
    if p is None:
        raise PermissionError("no principal")
    return p


def install(app) -> None:
    app.middleware("http")(_gate)
    app.middleware("http")(_headers)                 # 나중에 등록한 것이 바깥쪽: 모든 응답에 헤더가 붙는다

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        return _json(422, "요청 형식이 올바르지 않아요.")

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # 내부 예외 내용은 서버 로그에만 남기고 화면에는 내보내지 않는다.
        log.error("처리되지 않은 오류 %s %s: %s", request.method, request.url.path,
                  type(exc).__name__)
        return _json(500, GENERIC_ERROR)
