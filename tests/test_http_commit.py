"""HttpTransport 를 통한 실제 최종 커밋 경로 회귀 테스트.

버그: mediator/commit.py 의 finalize() 가 transport.agent(agent_id) 를
직접 호출했다. InProcessTransport 에는 이 메서드가 있었지만 HttpTransport
에는 없어서, 실제 분리된 에이전트 서버(HTTP)로 붙는 순간 AttributeError가
나고 "에이전트 없음(AGENT_UNAVAILABLE)"으로 오진됐다. 협상 테스트 101개가
전부 InProcessTransport 만 써서 이 경로는 검증되지 않았다.

이 테스트는 agent.server:app 을 실제 서브프로세스 3개로 띄우고(FakeCalendar
백엔드 — Google 계정 없이 결정론적으로 검증하기 위함, LocalAgent 이후의
로직은 GoogleCalendar 와 동일한 인터페이스를 탄다), HttpTransport 로
협상 → 승인 → 재확인 → 커밋까지 실제 네트워크를 태워 COMMITTED 에
도달하는지 확인한다.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from agent.profile import Profile
from common import config
from common.ids import group_pseudonym, new_session_id
from common.slots import build_slots, next_monday
from mediator import commit as commit_mod
from mediator import store
from mediator.negotiation import run_negotiation
from mediator.transport import HttpTransport

ROOT = Path(__file__).resolve().parent.parent
GROUP_ID = "test-http-commit"
KEYS = ["a", "b", "c"]

_tmp_db = None


def setup_function(fn=None):
    global _tmp_db
    if _tmp_db and Path(_tmp_db).exists():
        Path(_tmp_db).unlink()
    _tmp_db = tempfile.mktemp(suffix=".db")
    store.set_db_path(_tmp_db)
    store.init_db()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_agent(key: str, port: int) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(ROOT),
           "FAIRMEET_PROFILE": f"profiles/{key}.json",
           "FAIRMEET_FAKE": "1",
           "FAIRMEET_GROUP_ID": GROUP_ID}
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "agent.server:app",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_healthy(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"에이전트 서버가 {timeout}s 내에 기동하지 않음 (port={port})")


def test_http_transport_reaches_committed():
    """P0: 실제 HTTP 로 협상 완료 → 승인 → 재확인 → 커밋 → COMMITTED."""
    ports = {k: _free_port() for k in KEYS}
    procs = [_spawn_agent(k, ports[k]) for k in KEYS]
    try:
        for k in KEYS:
            _wait_healthy(ports[k])

        endpoints, agent_ids, profiles = {}, {}, {}
        for k in KEYS:
            prof = Profile.load(ROOT / "profiles" / f"{k}.json")
            aid = group_pseudonym(config.GROUP_SECRET, GROUP_ID, prof.user_key)
            endpoints[aid] = f"http://127.0.0.1:{ports[k]}"
            agent_ids[k] = aid
            profiles[k] = prof

        transport = HttpTransport(endpoints, config.AGENT_TOKEN)
        slots = build_slots(next_monday(), "work", 60)
        session_id = new_session_id()
        store.create_session(session_id, GROUP_ID, "work", 60, slots, [
            {"agent_id": agent_ids[k], "display_name": profiles[k].display_name,
             "email": profiles[k].email, "is_organizer": profiles[k].is_organizer}
            for k in KEYS])

        outcome = run_negotiation(session_id, transport, slots, GROUP_ID)
        assert outcome.state == "PENDING_HUMAN_APPROVAL", (
            f"협상이 승인 대기에 도달하지 못함: {outcome.state} {outcome.reason}")

        for p in store.get_participants(session_id):
            store.record_approval(session_id, p["agent_id"], "approve")

        res = commit_mod.finalize(session_id, transport)

        assert res.state == "COMMITTED", (
            f"실제 HTTP 최종 커밋 경로가 COMMITTED에 도달하지 못함: "
            f"{res.state} {res.reason}")
        assert res.event_id
        assert res.newly_created is True
        assert store.get_session(session_id)["google_event_id"] == res.event_id
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
