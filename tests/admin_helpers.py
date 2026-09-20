"""관리자 로그인이 필요한 테스트용 도우미. 테스트 함수는 여기에 없다.

with 블록 안에서만 관리자 자격정보가 설정되고(환경변수), 끝나면 원래대로(로컬 전용 모드) 돌아간다.
"""
from __future__ import annotations

import os

from mediator import auth

USER = "admin-test"
PASSWORD = "Correct-Horse-Battery-9!"
_HASH: str | None = None


def password_hash() -> str:
    global _HASH
    if _HASH is None:
        _HASH = auth.hash_password(PASSWORD)
    return _HASH


class AdminLogin:
    """client 를 관리자로 로그인시킨다 (쿠키 + CSRF 헤더). demo=True 면 시연 모드도 켠다."""

    HEADERS = {"X-FairMeet": "1"}

    def __init__(self, client, demo: bool = False):
        self.client, self.demo = client, demo
        self.csrf: str | None = None

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in ("FAIRMEET_ADMIN_USER", "FAIRMEET_ADMIN_PASSWORD_HASH")}
        os.environ["FAIRMEET_ADMIN_USER"] = USER
        os.environ["FAIRMEET_ADMIN_PASSWORD_HASH"] = password_hash()
        auth.reset_state()
        self.old_headers = dict(self.client.headers)
        r = self.client.post("/api/login", json={"username": USER, "password": PASSWORD},
                             headers=self.HEADERS)
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrfToken"]
        self.client.headers["X-CSRF-Token"] = self.csrf
        if self.demo:
            assert self.client.post("/api/demo-mode", json={"enabled": True}).json()["demoMode"] is True
        return self

    def __exit__(self, *exc):
        self.client.cookies.clear()
        self.client.headers.clear()
        self.client.headers.update(self.old_headers)
        for k, v in self.saved.items():
            os.environ[k] = v or ""
        auth.reset_state()
