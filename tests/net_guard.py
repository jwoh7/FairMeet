"""테스트 중 외부 네트워크 접근을 막는 가드.

실제 LLM API, 실제 이메일(SMTP), 실제 Google API 같은 외부 호출이 테스트에서 일어나면
즉시 실패시킨다. 로컬(127.0.0.1) 연결만 허용한다 — 테스트가 띄우는 agent 서버, TestClient 등.

conftest.py(pytest)와 run_tests.py(대체 러너)가 함께 쓴다.
"""
from __future__ import annotations

import socket

_LOCAL = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_installed = False
attempts: list[str] = []          # 차단된 접속 시도 (검증용)


class ExternalNetworkBlocked(RuntimeError):
    pass


def _check(address) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if isinstance(host, str) and host not in _LOCAL and not host.startswith("127."):
        attempts.append(host)
        raise ExternalNetworkBlocked(f"테스트 중 외부 네트워크 접근 시도가 차단되었습니다: {host}")


def _connect(self, address):
    _check(address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check(address)
    return _real_connect_ex(self, address)


def install() -> None:
    global _installed
    if not _installed:
        socket.socket.connect = _connect
        socket.socket.connect_ex = _connect_ex
        _installed = True


def uninstall() -> None:
    global _installed
    if _installed:
        socket.socket.connect = _real_connect
        socket.socket.connect_ex = _real_connect_ex
        _installed = False
