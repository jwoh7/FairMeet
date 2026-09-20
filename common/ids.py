"""그룹별 가명 ID 생성.

안정적인 무염 SHA-256 은 같은 사용자가 모든 그룹에서 같은 ID를 갖게 되어
그룹 간 상관관계 추적이 가능하다. 그룹 비밀키로 HMAC을 걸면
같은 사람이라도 회사 그룹과 친구 그룹에서 서로 다른 ID를 갖는다.
"""
from __future__ import annotations

import hashlib
import hmac


def group_pseudonym(group_secret: str, group_id: str, user_key: str) -> str:
    """그룹 내에서만 유효한 가명을 만든다.

    형식: <그룹태그>_<12자리 hex>
    예:   grp7a3f_b81c29d4e5f6

    payload_guard 의 agentId 정규식과 형식이 일치해야 한다.
    """
    gtag = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:4]
    mac = hmac.new(
        group_secret.encode("utf-8"),
        f"{group_id}|{user_key}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]
    return f"grp{gtag}_{mac}"


def new_session_id() -> str:
    """32자리 hex. payload_guard 의 sessionId 정규식과 맞춘다.

    Google Calendar 커스텀 event ID 는 base32hex(a-v, 0-9)만 허용하는데,
    hex 문자(0-9a-f)는 그 부분집합이므로 그대로 event ID 로 쓸 수 있다.
    """
    import secrets
    return secrets.token_hex(16)


def new_nonce() -> str:
    """replay 방지용 1회용 값. 16~32자리 hex."""
    import secrets
    return secrets.token_hex(8)
