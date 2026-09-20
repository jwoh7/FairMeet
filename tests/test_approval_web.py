"""승인 페이지(/approve/{token})와 관리자 API 의 HTTP 수준 테스트.

Part A 는 승인 라우터만, Part B 는 mediator.server.app 전체를 FakeCalendar +
FakeNotifier 로 돌린다. 실제 이메일도 실제 Google Calendar 도 쓰지 않는다.
"""
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.slots import format_slot
from mediator import approval_flow, store
from mediator import commit as commit_mod
from mediator import notifier as nm
from mediator.approval_web import STALE_TITLE, router
from mediator.notifier import FakeNotifier
from tests.admin_helpers import AdminLogin
from tests.flow_helpers import NAMES, MaxRounds, fresh_db, start_flow

PENDING = "PENDING_HUMAN_APPROVAL"


def setup_function(fn=None):
    fresh_db()


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _path(flow, name, version=None) -> str:
    return "/approve/" + flow.token_for(name, version)


def _wait(predicate, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---- Part A: 승인 라우터 --------------------------------------------------------

def test_link_page_shows_slot_and_does_not_consume_the_link():
    f = start_flow()
    c = _client()
    s, e = f.slots[f.slot_index]

    for _ in range(2):                                    # 메일 스캐너가 미리 열어도 소비되지 않는다
        r = c.get(_path(f, "민지"))
        assert r.status_code == 200
        assert format_slot(s, e) in r.text and "민지" in r.text
    assert store.approval_tally(f.sid) == (0, 0, 3)

    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "서연" not in r.text and "준호" not in r.text  # 다른 참가자 정보 없음
    assert "/approve/" not in r.text and "응답하지 않았는지는" in r.text


def test_post_records_once_and_reuse_shows_expired_message():
    f = start_flow()
    c = _client()
    p = _path(f, "민지")

    r1 = c.post(p, data={"verdict": "approve"})
    assert "동의가 기록되었습니다" in r1.text
    for _ in range(3):                                    # 새로고침·재전송
        r = c.post(p, data={"verdict": "approve"})
        assert r.status_code == 200 and STALE_TITLE in r.text
    assert STALE_TITLE in c.get(p).text                    # 사용한 링크는 페이지도 열리지 않는다
    assert STALE_TITLE in c.post(p, data={"verdict": "reject"}).text
    assert store.approval_tally(f.sid) == (1, 0, 3)
    assert f.version == 1


def test_reject_via_web_reopens_and_old_links_show_expired_message():
    f = start_flow()
    c = _client()
    calls = []
    approval_flow.configure(on_new_round=calls.append)
    old_minji = _path(f, "민지")

    r = c.post(_path(f, "준호"), data={"verdict": "reject"})
    assert "거절이 접수되었습니다" in r.text and "이유는 다른 참가자에게 전달되지 않습니다" in r.text
    assert calls == [f.sid]                                # 새 제안 이메일을 예약했다
    assert f.version == 2

    assert STALE_TITLE in c.get(old_minji).text            # 이전 제안의 링크
    assert STALE_TITLE in c.post(old_minji, data={"verdict": "approve"}).text
    assert store.approval_tally(f.sid) == (0, 0, 3)

    f.dispatch()
    r2 = c.get(_path(f, "민지"))
    s, e = f.slots[f.slot_index]
    assert format_slot(s, e) in r2.text and "새 제안" in r2.text


def test_bad_tokens_all_show_the_same_expired_message():
    f = start_flow()
    c = _client()
    good = f.token_for("민지")
    body, sig = good.split(".")
    for bad in ("garbage", "a.b", body + ".AAAA", sig + "." + body, "x" * 600):
        g = c.get(f"/approve/{bad}")
        p = c.post(f"/approve/{bad}", data={"verdict": "approve"})
        assert g.status_code == 200 and p.status_code == 200
        assert STALE_TITLE in g.text and STALE_TITLE in p.text
    assert store.approval_tally(f.sid) == (0, 0, 3)
    assert c.post(f"/approve/{good}", data={"verdict": "maybe"}).status_code == 400


def test_last_approval_schedules_finalize_and_creates_exactly_one_event():
    f = start_flow()
    c = _client()
    fired = []
    approval_flow.configure(on_all_approved=fired.append)

    texts = [c.post(_path(f, n), data={"verdict": "approve"}).text for n in NAMES]
    assert "동의가 기록되었습니다" in texts[0] and "동의가 기록되었습니다" in texts[1]
    assert "전원 동의 완료" in texts[2]
    assert fired == [f.sid]                                # 정확히 한 번

    assert commit_mod.finalize(f.sid, f.transport).state == "COMMITTED"
    assert f.created_events() == 1


def test_final_rejection_shows_termination_message():
    with MaxRounds(1):
        f = start_flow()
        r = _client().post(_path(f, "준호"), data={"verdict": "reject"})
    assert "거절이 접수되었습니다" in r.text and "종료" in r.text
    assert f.session["state"] == "MAX_APPROVAL_ROUNDS_REACHED"


# ---- Part B: 서버 전체 (FakeCalendar + FakeNotifier) ------------------------------------

class _FakeServer:
    """mediator.server 를 FAKE 캘린더 모드로 돌리고, 끝나면 원래대로 돌려놓는다."""

    def __enter__(self):
        from mediator import server
        self.server = server
        self.saved = (server.USE_FAKE, server._transport, dict(server._agent_names))
        fresh_db()
        server.USE_FAKE = True
        server._transport = None
        server._agent_names.clear()
        server._build_transport()
        self.notifier = FakeNotifier()
        nm.set_notifier(self.notifier)
        approval_flow.configure(emit=server._broadcast,
                                on_all_approved=server.schedule_finalize,
                                on_new_round=server.schedule_dispatch)
        self.client = TestClient(server.app)
        return self

    def __exit__(self, *a):
        srv = self.server
        srv.join_background()
        srv.USE_FAKE, srv._transport, names = self.saved
        srv._agent_names.clear()
        srv._agent_names.update(names)
        approval_flow.reset_hooks()
        nm.set_notifier(FakeNotifier())

    def events(self) -> int:
        return sum(len(a.backend.created_events)
                   for a in self.server._transport._agents.values())

    def post_link(self, message, verdict):
        return self.client.post("/approve/" + self.notifier.token_of(message),
                                data={"verdict": verdict})


def test_http_negotiate_emails_links_and_hides_them_from_the_dashboard():
    with _FakeServer() as fx:
        r = fx.client.post("/negotiate", json={})
        j = r.json()
        assert r.status_code == 200 and j["state"] == PENDING and not j["needsAttention"]
        assert "approvalLinks" not in r.text and "/approve/" not in r.text

        out = fx.notifier.outbox
        assert len(out) == 3 and len({m.approval_url for m in out}) == 3
        tokens = [fx.notifier.token_of(m) for m in out]

        dash = fx.client.get(f"/session/{j['sessionId']}/dashboard")
        blob = dash.text
        assert dash.status_code == 200
        assert all(t not in blob for t in tokens) and "/approve/" not in blob
        assert all(m.to_email not in blob for m in out)
        assert {p["label"] for p in dash.json()["participants"]} == {"발송됨"}

        page = fx.client.get("/").text
        assert "approvalLinks" not in page and "/approve/" not in page
        assert fx.client.get("/health").json()["email_backend"] == "fake"
        assert fx.client.get("/sessions/latest").json()["sessionId"] == j["sessionId"]


def test_http_full_flow_reject_then_reproposal_then_single_event():
    with _FakeServer() as fx:
        j = fx.client.post("/negotiate", json={}).json()
        sid = j["sessionId"]
        v1 = {m.to_email: m for m in fx.notifier.outbox}
        first_slot = j["slot"]
        first, second, third = list(v1.values())

        assert "동의가 기록" in fx.post_link(first, "approve").text
        assert "동의가 기록" in fx.post_link(second, "approve").text
        assert "거절이 접수" in fx.post_link(third, "reject").text

        assert _wait(lambda: len(fx.notifier.outbox) == 6), "새 제안 이메일이 발송되지 않음"
        dash = fx.client.get(f"/session/{sid}/dashboard").json()
        assert dash["timelineText"] == "1순위 거절 → 2순위 재제안"
        assert dash["currentSlot"] != first_slot and dash["state"] == PENDING
        assert fx.events() == 0

        # 이전 링크는 죽었다
        assert STALE_TITLE in fx.client.get("/approve/" + fx.notifier.token_of(first)).text
        v2 = [m for m in fx.notifier.outbox if m.proposal_version == 2]
        assert {m.to_email for m in v2} == set(v1)
        assert all(m.approval_url not in {x.approval_url for x in v1.values()} for m in v2)

        texts = [fx.post_link(m, "approve").text for m in v2]   # 전원이 새 시간에 다시 동의해야 한다
        assert "동의가 기록" in texts[0] and "동의가 기록" in texts[1]
        assert "전원 동의 완료" in texts[2]
        assert _wait(lambda: store.get_session(sid)["state"] == "COMMITTED"), \
            store.get_session(sid)["state"]
        assert fx.events() == 1                            # Google 이벤트는 정확히 1개
        # 잔액 갱신은 COMMITTED 전이 직후에 이어서 일어난다 (백그라운드): 끝나기를 기다린 뒤 센다
        assert _wait(lambda: store.get_session(sid)["balance_applied"] == 1)
        assert len(store.get_history(fx.server.GROUP_ID)) == 3


def test_http_email_failure_is_visible_and_retryable():
    with _FakeServer() as fx:
        fx.notifier.fail_all = True
        fx.notifier.fail_message = "SMTPServerDisconnected: 연결 끊김"
        j = fx.client.post("/negotiate", json={}).json()
        sid = j["sessionId"]
        assert j["state"] == PENDING and j["needsAttention"] is True
        assert len(j["emailFailures"]) == 3 and "연결 끊김" in j["emailFailures"][0]["error"]
        assert j["notification"]["status"] == "all_failed"
        assert fx.notifier.outbox == [] and fx.events() == 0

        dash = fx.client.get(f"/session/{sid}/dashboard").json()
        assert {p["label"] for p in dash["participants"]} == {"발송 실패"}
        assert dash["notification"]["retryable"] is True

        fx.notifier.fail_all = False
        rr = fx.client.post(f"/session/{sid}/notifications/retry")
        assert rr.status_code == 200 and rr.json()["sent"] == 3 and rr.json()["failed"] == 0
        assert len(fx.notifier.outbox) == 3
        again = fx.client.post(f"/session/{sid}/notifications/retry").json()
        assert again["sent"] == 0 and len(fx.notifier.outbox) == 3    # 이미 발송된 건 재발송 없음
        dash = fx.client.get(f"/session/{sid}/dashboard").json()
        assert {p["label"] for p in dash["participants"]} == {"발송됨"}


def test_http_terminal_state_offers_choices_and_sends_nothing_more():
    with _FakeServer() as fx, MaxRounds(1):
        j = fx.client.post("/negotiate", json={}).json()
        sid = j["sessionId"]
        assert "거절이 접수" in fx.post_link(fx.notifier.outbox[0], "reject").text
        assert store.get_session(sid)["state"] == "MAX_APPROVAL_ROUNDS_REACHED"
        sent = len(fx.notifier.outbox)

        d = fx.client.get(f"/session/{sid}/dashboard").json()
        assert d["terminal"] and [o["action"] for o in d["outcome"]["options"]] == \
            ["widen_range", "shorten_duration", "close"]
        assert "MAX_APPROVAL" not in d["outcome"]["title"]         # 기술 용어 대신 사용자 문장

        retry = fx.client.post(f"/session/{sid}/notifications/retry")
        assert retry.status_code == 409 and len(fx.notifier.outbox) == sent
        assert fx.events() == 0
        assert fx.client.get("/ledger").status_code == 403       # 시연 모드가 아니면 원장은 서버가 내주지 않는다
        with AdminLogin(fx.client, demo=True):
            entries = fx.client.get("/ledger").json()["entries"]
        assert entries and all(abs(e["balance"]) < 1e-9 for e in entries)   # 잔액 그대로

        wide =fx.client.post(f"/session/{sid}/followup", json={"action": "widen_range"}).json()
        slots = store.get_slots(wide["sessionId"])
        assert wide["sessionId"] != sid and (slots[-1][0] - slots[0][0]).days >= 9
        short = fx.client.post(f"/session/{sid}/followup",
                               json={"action": "shorten_duration"}).json()
        assert store.get_session(short["sessionId"])["duration_min"] == 30

        assert fx.client.post(f"/session/{sid}/followup", json={"action": "close"}).json() \
            == {"closed": True}
        bad = fx.client.post(f"/session/{sid}/followup", json={"action": "nonsense"})
        assert bad.status_code == 409
        running = fx.client.post(f"/session/{short['sessionId']}/followup",
                                 json={"action": "widen_range"})
        assert running.status_code == 409                          # 진행 중인 협상에는 적용 불가
        assert fx.client.post("/negotiate", json={"days": 99}).status_code == 400
        assert fx.client.post("/negotiate", json={"mode": "party"}).status_code == 400
