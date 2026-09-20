"""회귀 테스트 (P0-2): 참가자가 변경 요청을 제출했는데 관리자 화면에는 계속 "변경 요청이 없어요"만
보이던 문제.

실제 원인: 참가자의 제출은 `change_intakes` 에 저장되는데(v5), 관리자 화면에 내려가는
`present.meeting_detail()` 의 change 페이로드는 `change_requests`(= 주최자가 처리 방법을 고른 뒤에야
만들어지는 투표 행)만 담고 있었다. 그래서 접수된 요청은 어디에도 표시되지 않았고, 주최자가 처리
방법을 고를 수 있는 버튼도 화면에 없었다.

실제 이메일·Google Calendar 는 쓰지 않는다 (FakeServer: 임시 DB + FakeNotifier + FakeCalendar).
"""
import json
import re
import time
from pathlib import Path

from mediator import change_flow, store
from tests.server_helpers import FakeServer

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
NAMES = ["재원", "도연", "승현"]


def _wait(predicate, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _committed(fx):
    """협상 → 전원 승인 → 확정(COMMITTED, google_event_id 있음) → 확정 안내 메일까지."""
    sid = fx.client.post("/negotiate", json={}).json()["sessionId"]
    for n in NAMES:
        fx.post_link(fx.latest_mail(n), "approve")
    assert _wait(lambda: store.get_session(sid)["state"] == "COMMITTED")
    assert _wait(lambda: len(fx.notifier.mails("회의가 확정되었습니다")) == 3)
    assert store.get_session(sid)["google_event_id"]
    return sid


def _submit(fx, name):
    """참가자가 확정 안내 메일의 링크로 변경 요청만 보낸다 (처리 방법은 고르지 않는다)."""
    tok = fx.notifier.link_token(
        fx.notifier.mails("회의가 확정되었습니다", fx.agent(name).profile.email)[-1])
    r = fx.client.post(f"/change/request/{tok}", data={})
    return tok, r


def _intakes(sid, state="open"):
    with store.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM change_intakes WHERE session_id = ? AND state = ?", (sid, state))]


# ---- 제출 → DB → 관리자 API → 화면 계약 ----------------------------------------------------

def test_a_submitted_change_request_reaches_the_admin_detail_payload():
    with FakeServer() as fx:
        sid = _committed(fx)
        before = fx.client.get(f"/api/meetings/{sid}").json()
        assert before["change"]["intakes"] == [] and before["change"]["openIntakes"] == 0

        tok, r = _submit(fx, "도연")
        assert "변경 요청을 보냈어요" in r.text

        rows = _intakes(sid)
        assert len(rows) == 1 and rows[0]["decision"] is None      # pending 접수 정확히 1개

        d = fx.client.get(f"/api/meetings/{sid}").json()
        ch = d["change"]
        assert ch["openIntakes"] == 1 and len(ch["intakes"]) == 1
        card = ch["intakes"][0]
        assert card["intakeId"] == rows[0]["intake_id"]
        assert card["requester"] == "도연"                          # 주최자에게는 요청자를 보여준다
        assert card["slot"] and card["createdAt"]                    # 현재 일정 · 요청 시간
        assert {o["action"] for o in card["options"]} == {
            "renegotiate", "proceed_without", "cancel", "decline"}
        assert ch["open"] is True                                    # 처리 대기 중임이 드러난다

        # 결정 전에는 기존 Google 이벤트·세션이 그대로다
        assert fx.events() == 1 and store.get_session(sid)["state"] == "COMMITTED"
        assert fx.notifier.mails("일정 변경 요청에 대한 투표") == []


def test_the_admin_api_lists_the_open_intake():
    with FakeServer() as fx:
        sid = _committed(fx)
        _submit(fx, "승현")
        out = fx.client.get("/api/change-requests").json()["requests"]
        assert len(out) == 1 and out[0]["meetingId"] == sid
        assert out[0]["requester"] == "승현" and out[0]["state"] == "open"


def test_dashboard_renders_intake_cards_instead_of_the_empty_message():
    """app.js 계약: 접수가 있으면 '변경 요청이 없어요' 를 띄우지 않고 결정 버튼을 그린다."""
    m = re.search(r"function changeCard\(d\)\s*\{.*?\n\}", JS, re.S)
    assert m, "changeCard 함수를 찾지 못했습니다"
    body = m.group(0)
    assert "c.intakes" in body
    assert "!c.requests.length && !intakes.length" in body, "빈 메시지 조건이 접수를 고려하지 않습니다"
    assert "decideChange" in body and "/api/change-requests/" in JS
    # 선택지는 서버가 준 것을 그대로 쓴다 (프론트엔드가 처리 방법을 하드코딩하지 않는다)
    assert "o.action" in body and "o.enabled" in body and "o.reason" in body
    # 참가자 페이지(change_web)에는 처리 방법 선택이 없고, 결정은 관리자 API 로만 간다
    assert "/api/change-requests/${intakeId}/decide" in JS


def test_submitting_the_same_link_twice_keeps_one_intake():
    with FakeServer() as fx:
        sid = _committed(fx)
        tok, first = _submit(fx, "도연")
        assert "변경 요청을 보냈어요" in first.text
        again = fx.client.post(f"/change/request/{tok}", data={})
        assert "만료되었거나 이미 처리된" in again.text          # 링크는 1회용
        assert len(_intakes(sid)) == 1
        assert fx.client.get(f"/api/meetings/{sid}").json()["change"]["openIntakes"] == 1


def test_forged_or_expired_links_are_still_refused():
    with FakeServer() as fx:
        _committed(fx)
        tok, _ = _submit(fx, "도연")
        assert "만료되었거나 이미 처리된" in fx.client.get(
            f"/change/request/{tok[:-4]}AAAA").text
        assert "만료되었거나 이미 처리된" in fx.client.get("/change/request/garbage").text


def test_intakes_of_one_meeting_do_not_leak_into_another():
    with FakeServer() as fx:
        sid1 = _committed(fx)
        _submit(fx, "도연")
        # 두 번째 회의를 따로 확정하고, 첫 회의의 접수가 섞이지 않는지 본다
        sid2 = fx.client.post("/negotiate", json={}).json()["sessionId"]
        for n in NAMES:
            m = [x for x in fx.notifier.mails("회의 시간 승인 요청", fx.agent(n).profile.email)
                 if x.session_id == sid2][-1]
            fx.post_link(m, "approve")
        assert _wait(lambda: store.get_session(sid2)["state"] == "COMMITTED")

        d1 = fx.client.get(f"/api/meetings/{sid1}").json()["change"]
        d2 = fx.client.get(f"/api/meetings/{sid2}").json()["change"]
        assert d1["openIntakes"] == 1 and d2["openIntakes"] == 0
        assert d2["intakes"] == []
        assert "도연" not in json.dumps(d2, ensure_ascii=False)


def test_only_the_organizer_decision_moves_the_flow_forward():
    with FakeServer() as fx:
        sid = _committed(fx)
        _submit(fx, "도연")
        iid = _intakes(sid)[0]["intake_id"]

        # 결정 전: 투표 메일 없음, change_requests 행 없음
        with store.connect() as c:
            assert c.execute("SELECT COUNT(*) n FROM change_requests WHERE session_id = ?",
                             (sid,)).fetchone()["n"] == 0

        r = fx.client.post(f"/api/change-requests/{iid}/decide", json={"action": "cancel"})
        assert r.status_code == 200 and r.json()["status"] == "voting"
        assert _wait(lambda: len(fx.notifier.mails("일정 변경 요청에 대한 투표")) == 2)

        d = fx.client.get(f"/api/meetings/{sid}").json()["change"]
        assert d["openIntakes"] == 0                     # 처리됐으므로 대기 카드가 사라진다
        assert d["requests"] and d["requests"][0]["state"] == "voting"
        assert fx.events() == 1                          # 투표 통과 전에는 이벤트 그대로


def test_the_empty_message_only_shows_when_there_is_nothing():
    with FakeServer() as fx:
        sid = _committed(fx)
        ch = fx.client.get(f"/api/meetings/{sid}").json()["change"]
        assert ch["intakes"] == [] and ch["requests"] == [] and ch["open"] is False
