"""승인 링크용 서명 토큰.

토큰은 `<payload>.<서명>` 형태다. payload 에는 세션 ID, 제안 버전, 참가자,
만료 시각, 1회용 식별자(jti)가 들어 있고, 서명은 그룹 비밀키에서 파생한
키로 만든 HMAC-SHA256 이다.

서명은 '위조되지 않았음'만 증명한다. 아래 조건은 DB 상태와 대조해야 하며
approval_flow 가 한 트랜잭션 안에서 확인한다.
  - 이 제안 버전이 아직 현재 버전인가      (이전 제안의 링크 무효화)
  - jti 가 아직 사용/폐기되지 않았는가     (1회용)
  - 세션이 아직 승인 대기 상태인가

토큰 본문은 DB 에 저장하지 않는다. 그래서 DB 나 대시보드 응답에서
링크를 재구성할 수 없다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from common import config

_PURPOSE = b"fairmeet/approval-token/v1"


class TokenInvalid(ValueError):
    """서명이 틀렸거나 형식이 잘못되었거나 만료된 토큰."""


@dataclass(frozen=True)
class TokenClaims:
    session_id: str
    proposal_version: int
    agent_id: str
    expires_at: int          # epoch seconds
    jti: str


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _key() -> bytes:
    # 그룹 비밀키를 그대로 서명에 쓰지 않고 용도별로 파생한다.
    return hmac.new(config.GROUP_SECRET.encode("utf-8"), _PURPOSE,
                    hashlib.sha256).digest()


def _sign(body: str) -> str:
    return _b64e(hmac.new(_key(), body.encode("ascii"), hashlib.sha256).digest())


def new_jti() -> str:
    return secrets.token_urlsafe(16)


# ---- 일정 변경 링크 ----------------------------------------------------------------------------
# 승인 링크와 서명 키·payload 형식이 분리되어 있어서, 승인 토큰을 변경 링크로(또는 그 반대로)
# 쓸 수 없다. purpose: 'request'(변경 요청 링크) | 'vote'(변경 투표 링크)

_PURPOSE_CHANGE = b"fairmeet/change-token/v1"
CHANGE_PURPOSES = ("request", "vote")


@dataclass(frozen=True)
class ChangeClaims:
    purpose: str
    session_id: str
    request_id: str          # 요청 링크는 ''
    agent_id: str
    expires_at: int
    jti: str


def _change_key() -> bytes:
    return hmac.new(config.GROUP_SECRET.encode("utf-8"), _PURPOSE_CHANGE,
                    hashlib.sha256).digest()


def _change_sign(body: str) -> str:
    return _b64e(hmac.new(_change_key(), body.encode("ascii"), hashlib.sha256).digest())


def mint_change(purpose: str, session_id: str, request_id: str, agent_id: str,
                expires_at: int, jti: str | None = None) -> tuple[str, str]:
    """변경 링크 토큰을 만든다. Returns: (token, jti)"""
    if purpose not in CHANGE_PURPOSES:
        raise ValueError(f"알 수 없는 용도: {purpose}")
    jti = jti or new_jti()
    payload = {"p": purpose, "s": session_id, "r": request_id, "a": agent_id,
               "x": int(expires_at), "j": jti}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_change_sign(body)}", jti


def verify_change(token: str, now: float | None = None,
                  purpose: str | None = None) -> ChangeClaims:
    """서명·만료·용도를 검증한다. 실패하면 TokenInvalid (사유는 구분해 알리지 않는다)."""
    if not isinstance(token, str) or len(token) > 1024 or token.count(".") != 1:
        raise TokenInvalid("형식 오류")
    body, sig = token.split(".")
    try:
        body.encode("ascii")
        sig.encode("ascii")
    except UnicodeEncodeError as exc:
        raise TokenInvalid("형식 오류") from exc
    if not hmac.compare_digest(_change_sign(body), sig):
        raise TokenInvalid("서명 불일치")
    try:
        data = json.loads(_b64d(body))
        claims = ChangeClaims(
            purpose=str(data["p"]), session_id=str(data["s"]), request_id=str(data["r"]),
            agent_id=str(data["a"]), expires_at=int(data["x"]), jti=str(data["j"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenInvalid("payload 오류") from exc
    if claims.purpose not in CHANGE_PURPOSES or (purpose and claims.purpose != purpose):
        raise TokenInvalid("용도 불일치")
    if (now if now is not None else time.time()) >= claims.expires_at:
        raise TokenInvalid("만료")
    return claims


# ---- 회의 자료 열람 링크 ------------------------------------------------------------------------
# 승인·변경 링크와 서명 키·payload 형식이 분리되어 있어 서로 바꿔 쓸 수 없다. 1회용이 아니라 만료 시각까지
# 여러 번 열 수 있지만, 열 때마다 서버가 '지금도 이 회의의 참석자인가'를 다시 확인한다.

_PURPOSE_MATERIAL = b"fairmeet/material-token/v1"


@dataclass(frozen=True)
class MaterialClaims:
    prep_id: str
    session_id: str
    agent_id: str
    expires_at: int
    jti: str


def _material_sign(body: str) -> str:
    key = hmac.new(config.GROUP_SECRET.encode("utf-8"), _PURPOSE_MATERIAL, hashlib.sha256).digest()
    return _b64e(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())


def mint_material(prep_id: str, session_id: str, agent_id: str, expires_at: int,
                  jti: str | None = None) -> tuple[str, str]:
    jti = jti or new_jti()
    payload = {"p": prep_id, "s": session_id, "a": agent_id, "x": int(expires_at), "j": jti}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_material_sign(body)}", jti


def verify_material(token: str, now: float | None = None) -> MaterialClaims:
    if not isinstance(token, str) or len(token) > 1024 or token.count(".") != 1:
        raise TokenInvalid("형식 오류")
    body, sig = token.split(".")
    try:
        body.encode("ascii")
        sig.encode("ascii")
    except UnicodeEncodeError as exc:
        raise TokenInvalid("형식 오류") from exc
    if not hmac.compare_digest(_material_sign(body), sig):
        raise TokenInvalid("서명 불일치")
    try:
        data = json.loads(_b64d(body))
        claims = MaterialClaims(prep_id=str(data["p"]), session_id=str(data["s"]),
                                agent_id=str(data["a"]), expires_at=int(data["x"]),
                                jti=str(data["j"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenInvalid("payload 오류") from exc
    if (now if now is not None else time.time()) >= claims.expires_at:
        raise TokenInvalid("만료")
    return claims


def mint(session_id: str, proposal_version: int, agent_id: str,
         expires_at: int, jti: str | None = None) -> tuple[str, str]:
    """토큰을 만든다. Returns: (token, jti)"""
    jti = jti or new_jti()
    payload = {"s": session_id, "v": int(proposal_version), "a": agent_id,
               "x": int(expires_at), "j": jti}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True)
                 .encode("utf-8"))
    return f"{body}.{_sign(body)}", jti


def verify(token: str, now: float | None = None) -> TokenClaims:
    """서명과 만료를 검증한다. 실패하면 TokenInvalid.

    실패 사유는 호출자에게 구분해 알려주지 않는다 (화면에는 항상 같은
    문구가 나간다). 서명 비교는 상수 시간으로 한다.
    """
    if not isinstance(token, str) or len(token) > 1024 or token.count(".") != 1:
        raise TokenInvalid("형식 오류")
    body, sig = token.split(".")
    try:
        body.encode("ascii")
        sig.encode("ascii")
    except UnicodeEncodeError as exc:
        raise TokenInvalid("형식 오류") from exc

    if not hmac.compare_digest(_sign(body), sig):
        raise TokenInvalid("서명 불일치")

    try:
        data = json.loads(_b64d(body))
        claims = TokenClaims(
            session_id=str(data["s"]), proposal_version=int(data["v"]),
            agent_id=str(data["a"]), expires_at=int(data["x"]),
            jti=str(data["j"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise TokenInvalid("payload 오류") from exc

    if (now if now is not None else time.time()) >= claims.expires_at:
        raise TokenInvalid("만료")
    return claims
