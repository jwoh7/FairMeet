"""실제 에이전트 서버(HTTP 서브프로세스)의 새 기능: 기능 광고, 회의 전후 여유 옵션, 이벤트 수정·취소.

FakeCalendar 백엔드로 agent.server:app 을 서브프로세스로 띄워 진짜 HTTP 로 검증한다
(Google 계정·외부 네트워크 없음). 옛 에이전트(기능 광고·신규 엔드포인트 없음)와의 호환도 확인한다.
"""
import datetime as dt
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from agent.calendar_client import EventNotFound
from agent.profile import Profile
from common import config
from common.ids import group_pseudonym
from common.slots import KST, build_slots
from mediator.transport import AgentHttpError, AgentUnreachable, HttpTransport

ROOT = Path(__file__).resolve().parent.parent
GROUP_ID = "test-http-agent"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_agent(key: str, port: int) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(ROOT), "FAIRMEET_PROFILE": f"profiles/{key}.json",
           "FAIRMEET_FAKE": "1", "FAIRMEET_GROUP_ID": GROUP_ID}
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "agent.server:app",
                             "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def _wait_healthy(port: int, timeout: float = 20.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"에이전트 서버가 기동하지 않음 (port={port})")


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _dt(day, h, m=0):
    return dt.datetime(2026, 9, 21 + day, h, m, tzinfo=KST)


def test_real_agent_server_advertises_capabilities_and_applies_buffer_options():
    port = _free_port()
    proc = _spawn_agent("a", port)
    try:
        _wait_healthy(port)
        prof = Profile.load(ROOT / "profiles" / "a.json")
        aid = group_pseudonym(config.GROUP_SECRET, GROUP_ID, prof.user_key)
        t = HttpTransport({aid: f"http://127.0.0.1:{port}"}, config.AGENT_TOKEN)

        health = httpx.get(f"http://127.0.0.1:{port}/health").json()
        assert {"buffer", "event_update", "event_cancel"} <= set(health["capabilities"])
        assert t.capabilities(aid) >= {"buffer", "event_update", "event_cancel"}

        # 회의 전후 여유: 재원의 fake 캘린더에는 월 09:00~10:30 일정이 있다
        slots = build_slots(dt.date(2026, 9, 21), "work", 60)
        idx = next(i for i, (s, _) in enumerate(slots) if s == _dt(0, 10, 30))
        plain = t.open_negotiation(aid, "s" * 32, slots)["costs"]
        assert plain[idx] is not None                       # 일정이 10:30 에 끝나므로 붙어 있어도 가능
        hard = t.open_negotiation(aid, "t" * 32, slots, {
            "before_min": 30, "after_min": 0, "strength": "hard"})["costs"]
        assert hard[idx] is None                            # 앞 30분 여유 안에 일정이 있다
        soft = t.open_negotiation(aid, "u" * 32, slots, {
            "before_min": 30, "after_min": 0, "strength": "soft"})["costs"]
        assert soft[idx] == plain[idx] + 20.0

        # 잘못된 옵션은 400 (조용히 무시하지 않는다)
        for bad in ({"before_min": 9999}, {"before_min": "x"}, {"strength": "maybe",
                                                                   "before_min": 5}):
            try:
                t.open_negotiation(aid, "v" * 32, slots, bad)
            except AgentHttpError as exc:
                assert exc.status == 400
            else:
                raise AssertionError(f"잘못된 옵션이 통과함: {bad}")
    finally:
        _stop(proc)


def test_real_agent_server_updates_and_cancels_the_same_event():
    port = _free_port()
    proc = _spawn_agent("a", port)
    try:
        _wait_healthy(port)
        prof = Profile.load(ROOT / "profiles" / "a.json")
        aid = group_pseudonym(config.GROUP_SECRET, GROUP_ID, prof.user_key)
        t = HttpTransport({aid: f"http://127.0.0.1:{port}"}, config.AGENT_TOKEN)

        s1, e1 = _dt(1, 11), _dt(1, 12)                      # 재원의 fake 캘린더에서 비어 있는 시간
        s2, e2 = _dt(3, 10), _dt(3, 11)
        eid, created = t.commit(aid, event_id="fmabcde12345", summary="회의", start=s1, end=e1,
                                attendee_emails=["a@x.com", "b@x.com"], description="d")
        assert created is True
        assert t.recheck(aid, s1, e1) is True and t.recheck(aid, s2, e2) is False

        # 같은 이벤트의 시간과 참석자를 고친다 (새 이벤트가 아니다)
        assert t.update_event(aid, event_id=eid, start=s2, end=e2,
                              attendee_emails=["a@x.com"]) is True
        assert t.recheck(aid, s1, e1) is False and t.recheck(aid, s2, e2) is True
        again, created2 = t.commit(aid, event_id=eid, summary="회의", start=s2, end=e2,
                                   attendee_emails=["a@x.com"])
        assert (again, created2) == (eid, False)            # 같은 ID 는 멱등: 새로 만들지 않는다

        # 없는 이벤트: 수정은 EventNotFound, 취소는 False(멱등)
        try:
            t.update_event(aid, event_id="fmnothere000", start=s1, end=e1)
        except EventNotFound:
            pass
        else:
            raise AssertionError("없는 이벤트 수정이 성공함")
        assert t.cancel_event(aid, "fmnothere000") is False

        assert t.cancel_event(aid, eid) is True
        assert t.cancel_event(aid, eid) is False            # 다시 취소해도 안전
        assert t.recheck(aid, s2, e2) is False              # 캘린더가 비었다

        # 형식이 잘못된 요청
        for bad in ({"eventId": "x", "start": "2026-09-21T10:00:00+09:00"}, {"start": "bad"}, {}):
            r = httpx.post(f"http://127.0.0.1:{port}/rpc/event/update", json=bad,
                           headers={"Authorization": f"Bearer {config.AGENT_TOKEN}"})
            assert r.status_code == 400
        r = httpx.post(f"http://127.0.0.1:{port}/rpc/event/cancel", json={"eventId": "x"})
        assert r.status_code == 401                         # 인증 없이는 불가
    finally:
        _stop(proc)


class _OldAgent(http.server.BaseHTTPRequestHandler):
    """기능 광고도 이벤트 수정/취소 엔드포인트도 없는 옛 에이전트."""

    def do_GET(self):
        body = json.dumps({"status": "ok", "backend": "google"}).encode()
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body if self.path == "/health" else b'{"detail":"Not Found"}')

    def do_POST(self):
        self.send_response(404)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"detail":"Not Found"}')

    def log_message(self, *a):
        pass


def test_old_agents_are_treated_as_unsupported_not_as_supporting():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _OldAgent)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        t = HttpTransport({"old": f"http://127.0.0.1:{srv.server_address[1]}"}, "tok",
                          timeout=3.0)
        assert t.capabilities("old") == set()               # 광고가 없으면 지원하지 않는 것이다
        try:
            t.update_event("old", event_id="x", start=_dt(0, 9), end=_dt(0, 10))
        except AgentUnreachable as exc:
            assert "지원하지 않습니다" in str(exc) and "다시 시작" in str(exc)
        else:
            raise AssertionError("옛 에이전트에 대한 이벤트 수정이 성공함")
        try:
            t.cancel_event("old", "x")
        except AgentUnreachable as exc:
            assert "지원하지 않습니다" in str(exc)
        else:
            raise AssertionError("옛 에이전트에 대한 이벤트 취소가 성공함")
    finally:
        srv.shutdown()
        srv.server_close()

    dead = HttpTransport({"x": f"http://127.0.0.1:{_free_port()}"}, "tok", timeout=1.0)
    assert dead.capabilities("x") is None                   # 응답이 없으면 알 수 없음
