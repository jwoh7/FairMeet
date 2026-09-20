"""관리자 인증: 비밀번호 해시, 로그인 세션, CSRF, 속도 제한.

원칙
  - 자격정보는 환경변수로만 받는다 (FAIRMEET_ADMIN_USER, FAIRMEET_ADMIN_PASSWORD_HASH).
    평문 비밀번호는 코드·설정·로그·DB 어디에도 두지 않는다. 기본 비밀번호는 없다.
  - 자격정보가 설정되어 있으면 로그인 세션이 필요하다 (이 컴퓨터에서 접속해도 마찬가지).
  - 설정되어 있지 않으면 '로컬 전용 모드': 이 컴퓨터에서 프록시 없이 직접 연결한 요청만 관리자 기능을
    쓸 수 있다. 터널(Cloudflare)을 거친 요청은 프록시 헤더와 Host 로 구별되어 항상 거절된다.
  - 세션 ID 는 서버가 발급한 무작위 값이고 서버 메모리에만 있다 (서버를 재시작하면 다시 로그인).
    쿠키는 HttpOnly + SameSite=Strict, HTTPS 로 접속하면 Secure(__Host- 접두사)다.
  - 상태를 바꾸는 요청은 CSRF 토큰(로그인 세션)과 Origin 검사를 통과해야 한다.
  - 로그인 실패는 IP 별, 전체 별로 세어 잠근다. 사용자 이름이 틀려도 같은 시간이 걸리고 같은 문구다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from common import config

COOKIE_PLAIN = "fm_session"
COOKIE_SECURE = "__Host-fm_session"
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD_LEN = 12
_WEAK = {"password1234", "123456789012", "qwertyuiop12", "fairmeet1234", "adminadmin12",
         "passwordpassword", "changeme1234"}

PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "cf-visitor", "cf-ipcountry", "x-forwarded-for",
                 "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip")
_LOOPBACK_PEERS = {"127.0.0.1", "::1", "localhost"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_test_peers: set[str] = set()
_test_hosts: set[str] = set()


def trust_test_client() -> None:
    """테스트 하네스(TestClient)를 '이 컴퓨터에서 직접 접속'으로 취급한다. 테스트 패키지만 호출한다."""
    _test_peers.add("testclient")
    _test_hosts.add("testserver")


# ---- 비밀번호 --------------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                        p=SCRYPT_P, dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def _parse_hash(stored: str):
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return None
        return int(n), int(r), int(p), base64.b64decode(salt, validate=True), \
            base64.b64decode(dk, validate=True)
    except (ValueError, TypeError):
        return None


_DUMMY_HASH = hash_password("x" * 20, salt=b"\x00" * 16)


def verify_password(password: str, stored: str) -> bool:
    """상수 시간 비교. 저장된 해시가 깨져 있으면 더미 해시로 같은 계산을 하고 False."""
    parsed = _parse_hash(stored or "")
    if parsed is None or not (2 ** 12 <= parsed[0] <= 2 ** 20) or parsed[1] > 16 or parsed[2] > 4:
        parsed = _parse_hash(_DUMMY_HASH)
        ok_format = False
    else:
        ok_format = True
    n, r, p, salt, dk = parsed
    try:
        got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                             dklen=len(dk), maxmem=128 * 1024 * 1024)
    except ValueError:
        return False
    return hmac.compare_digest(got, dk) and ok_format


def password_problem(password: str, username: str = "") -> str | None:
    """관리자 비밀번호로 쓸 수 없는 이유 (없으면 None). 비밀번호 자체는 어디에도 남기지 않는다."""
    if len(password) < MIN_PASSWORD_LEN:
        return f"비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다."
    if password.lower() in _WEAK or (username and password.lower() == username.lower()):
        return "너무 쉬운 비밀번호입니다."
    if len({c for c in password}) < 6:
        return "서로 다른 문자를 더 많이 섞어 주세요."
    return None


# ---- 설정 -------------------------------------------------------------------------------------

def admin_user() -> str:
    return (os.environ.get("FAIRMEET_ADMIN_USER") or "").strip()


def admin_hash() -> str:
    return (os.environ.get("FAIRMEET_ADMIN_PASSWORD_HASH") or "").strip()


def is_configured() -> bool:
    return bool(admin_user()) and _parse_hash(admin_hash()) is not None


# ---- 속도 제한 --------------------------------------------------------------------------------

class FailureLimiter:
    """window 초 안에 max_fail 번 실패하면 lock 초 동안 잠근다."""

    def __init__(self, max_fail: int, window: float, lock: float):
        self.max_fail, self.window, self.lock = max_fail, window, lock
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._mu = threading.Lock()

    def blocked(self, key: str, now: float | None = None) -> int:
        now = time.time() if now is None else now
        with self._mu:
            until = self._locked.get(key, 0.0)
            if until > now:
                return int(until - now) + 1
            self._locked.pop(key, None)
            return 0

    def fail(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._mu:
            if len(self._fails) > 5000:
                self._fails.clear()
                self._locked.clear()
            hits = [t for t in self._fails.get(key, []) if now - t < self.window] + [now]
            self._fails[key] = hits
            if len(hits) >= self.max_fail:
                self._locked[key] = now + self.lock
                self._fails[key] = []

    def reset(self, key: str) -> None:
        with self._mu:
            self._fails.pop(key, None)
            self._locked.pop(key, None)

    def clear(self) -> None:
        with self._mu:
            self._fails.clear()
            self._locked.clear()


class RateLimiter:
    """window 초 안에 limit 번까지만 허용한다."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, list[float]] = {}
        self._mu = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._mu:
            if len(self._hits) > 5000:
                self._hits.clear()
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def clear(self) -> None:
        with self._mu:
            self._hits.clear()


LOGIN_BY_IP = FailureLimiter(max_fail=5, window=600, lock=600)
LOGIN_GLOBAL = FailureLimiter(max_fail=30, window=600, lock=300)
PUBLIC_LINKS = RateLimiter(limit=120, window=60)            # 참가자 링크 페이지 (IP 별)
MEETING_REQUESTS = RateLimiter(limit=20, window=60)         # 자연어 회의 요청 (관리자 별)
ABUSE = FailureLimiter(max_fail=8, window=600, lock=600)    # 차단된 입력이 반복되면 잠깐 멈춘다


def reset_state() -> None:
    """테스트용: 세션과 모든 제한 기록을 비운다."""
    SESSIONS.clear()
    for lim in (LOGIN_BY_IP, LOGIN_GLOBAL, ABUSE):
        lim.clear()
    for lim in (PUBLIC_LINKS, MEETING_REQUESTS):
        lim.clear()


# ---- 세션 -------------------------------------------------------------------------------------

@dataclass
class Session:
    sid: str
    user: str
    csrf: str
    created: float
    last: float
    demo: bool = False


class SessionStore:
    MAX = 20

    def __init__(self):
        self._by_sid: dict[str, Session] = {}
        self._mu = threading.Lock()

    def create(self, user: str, now: float | None = None) -> Session:
        now = time.time() if now is None else now
        s = Session(sid=secrets.token_urlsafe(32), user=user, csrf=secrets.token_urlsafe(24),
                    created=now, last=now)
        with self._mu:
            self._prune(now)
            while len(self._by_sid) >= self.MAX:
                oldest = min(self._by_sid.values(), key=lambda x: x.last)
                self._by_sid.pop(oldest.sid, None)
            self._by_sid[s.sid] = s
        return s

    def _prune(self, now: float) -> None:
        for sid, s in list(self._by_sid.items()):
            if self._expired(s, now):
                self._by_sid.pop(sid, None)

    @staticmethod
    def _expired(s: Session, now: float) -> bool:
        return (now - s.created > config.ADMIN_SESSION_MIN * 60
                or now - s.last > config.ADMIN_IDLE_MIN * 60)

    def get(self, sid: str | None, now: float | None = None) -> Session | None:
        if not sid or len(sid) > 128:
            return None
        now = time.time() if now is None else now
        with self._mu:
            s = self._by_sid.get(sid)
            if s is None:
                return None
            if self._expired(s, now):
                self._by_sid.pop(sid, None)
                return None
            s.last = now
            return s

    def destroy(self, sid: str | None) -> None:
        with self._mu:
            self._by_sid.pop(sid or "", None)

    def clear(self) -> None:
        with self._mu:
            self._by_sid.clear()


SESSIONS = SessionStore()


# ---- 요청 판별 --------------------------------------------------------------------------------

def _valid_ip(text: str) -> str | None:
    try:
        return str(ipaddress.ip_address(text.strip()))
    except ValueError:
        return None


def peer_of(request) -> str:
    return (request.client.host if request.client else "") or ""


def _peer_is_loopback(request) -> bool:
    return peer_of(request) in _LOOPBACK_PEERS | _test_peers


def client_ip(request) -> str:
    """제한 기록에 쓰는 접속자 주소. 프록시 헤더는 이 컴퓨터의 프록시(터널)가 붙인 것일 때만 믿는다."""
    peer = peer_of(request)
    if _peer_is_loopback(request):
        forwarded = request.headers.get("cf-connecting-ip") or ""
        ip = _valid_ip(forwarded)
        if ip:
            return ip
    return peer or "unknown"


def host_of(request) -> str:
    raw = (request.headers.get("host") or "").strip().lower()
    if raw.startswith("["):
        return raw.split("]")[0] + "]"
    return raw.split(":")[0]


def is_direct_local(request) -> bool:
    """이 컴퓨터에서, 프록시를 거치지 않고, 로컬 주소로 접속한 요청인가.

    터널로 들어온 요청은 연결 자체는 127.0.0.1 이지만 프록시 헤더가 붙고 Host 가 공개 주소다."""
    if not _peer_is_loopback(request):
        return False
    if any(h in request.headers for h in PROXY_HEADERS):
        return False
    return host_of(request) in _LOOPBACK_HOSTS | _test_hosts


def is_secure_request(request) -> bool:
    if request.url.scheme == "https":
        return True
    if _peer_is_loopback(request):
        if request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https":
            return True
        if "https" in request.headers.get("cf-visitor", "").lower():
            return True
    return False


def origin_ok(request) -> bool:
    """다른 사이트가 브라우저를 시켜 보낸 요청이면 False. Origin 이 없는 요청(curl, 서버 간)은 통과한다."""
    site = (request.headers.get("sec-fetch-site") or "").lower()
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:
        return False
    if not netloc:
        return False
    allowed = {(request.headers.get("host") or "").lower()}
    base = urlsplit(config.PUBLIC_BASE_URL or "").netloc.lower()
    if base:
        allowed.add(base)
    return netloc in allowed


# ---- 주체 -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    user: str
    authenticated: bool                 # False = 로컬 전용 모드의 이 컴퓨터 사용자
    session: Session | None = None

    @property
    def key(self) -> str:
        """제한 기록·감사 기록에서 이 주체를 가리키는 값."""
        return f"admin:{self.user}" if self.authenticated else "local"

    @property
    def csrf(self) -> str | None:
        return self.session.csrf if self.session else None

    @property
    def demo(self) -> bool:
        return bool(self.session and self.session.demo)


LOCAL = Principal(user="local", authenticated=False)


def session_cookie(request) -> str | None:
    return request.cookies.get(COOKIE_SECURE) or request.cookies.get(COOKIE_PLAIN)


def authenticate(request) -> Principal | None:
    if is_configured():
        s = SESSIONS.get(session_cookie(request))
        return Principal(user=s.user, authenticated=True, session=s) if s else None
    return LOCAL if is_direct_local(request) else None


def csrf_ok(request, principal: Principal) -> bool:
    """상태를 바꾸는 요청(POST 등)의 CSRF 검사."""
    if not origin_ok(request):
        return False
    if principal.authenticated:
        given = request.headers.get("x-csrf-token") or ""
        return bool(principal.csrf) and hmac.compare_digest(given, principal.csrf)
    return True


# ---- 로그인 -----------------------------------------------------------------------------------

@dataclass
class LoginResult:
    ok: bool
    session: Session | None = None
    retry_after: int = 0
    message: str = ""


_LOGIN_FAIL = "아이디 또는 비밀번호가 올바르지 않아요."


def login(username: str, password: str, ip: str) -> LoginResult:
    wait = max(LOGIN_BY_IP.blocked(ip), LOGIN_GLOBAL.blocked("*"))
    if wait:
        return LoginResult(False, retry_after=wait,
                           message="시도가 너무 많아요. 잠시 후 다시 시도해 주세요.")
    configured = is_configured()
    user_ok = configured and hmac.compare_digest(
        (username or "").encode("utf-8"), admin_user().encode("utf-8"))
    # 사용자 이름이 틀려도 같은 계산을 해서 응답 시간으로 알아낼 수 없게 한다.
    pw_ok = verify_password(password or "", admin_hash() if configured else "")
    if not (configured and user_ok and pw_ok):
        LOGIN_BY_IP.fail(ip)
        LOGIN_GLOBAL.fail("*")
        return LoginResult(False, message=_LOGIN_FAIL)
    LOGIN_BY_IP.reset(ip)
    return LoginResult(True, session=SESSIONS.create(admin_user()))


def set_session_cookie(response, request, session: Session) -> None:
    secure = is_secure_request(request)
    response.set_cookie(COOKIE_SECURE if secure else COOKIE_PLAIN, session.sid,
                        max_age=config.ADMIN_SESSION_MIN * 60, path="/", secure=secure,
                        httponly=True, samesite="strict")
    # 반대 이름의 쿠키가 남아 있으면 지운다 (http ↔ https 를 오갈 때 혼동 방지)
    response.delete_cookie(COOKIE_PLAIN if secure else COOKIE_SECURE, path="/")


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_PLAIN, path="/")
    response.delete_cookie(COOKIE_SECURE, path="/")
