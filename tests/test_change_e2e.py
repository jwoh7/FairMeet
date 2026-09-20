"""일정 변경을 서버 전체(HTTP 페이지 + 관리자 API + 백그라운드 스레드)로 검증한다 (v5).

협상 → 이메일 링크 승인 → 확정 → 확정 안내 메일 → 참가자의 변경 요청 접수(/change/request) →
주최자의 처리 방법 선택(/api/change-requests/{id}/decide) → 투표 메일/페이지 →
적용(취소 / 재협상) → 결과 메일. FakeCalendar·FakeNotifier 만 쓴다.
"""
import json
import time

from mediator import store
from tests.server_helpers import FakeServer

PENDING = "PENDING_HUMAN_APPROVAL"
NAMES = ["재원", "도연", "승현"]                       # profiles/a,b,c 의 표시 이름 (재원 = 주최자)


def _wait(predicate, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _committed(fx):
    j = fx.client.post("/negotiate", json={}).json()
    sid = j["sessionId"]
    for n in NAMES:
        fx.post_link(fx.latest_mail(n), "approve")
    assert _wait(lambda: store.get_session(sid)["state"] == "COMMITTED")
    assert _wait(lambda: len(fx.notifier.mails("회의가 확정되었습니다")) == 3)    # 백그라운드 발송
    return sid


def _link(fx, subject, name, index=-1):
    m = fx.notifier.mails(subject, fx.agent(name).profile.email)[index]
    return fx.notifier.link_token(m)


def _state(rid):
    with store.connect() as c:
        return c.execute("SELECT state FROM change_requests WHERE request_id = ?",
                         (rid,)).fetchone()["state"]


def _open_request(sid):
    with store.connect() as c:
        r = c.execute("SELECT request_id FROM change_requests WHERE session_id = ? "
                      "ORDER BY created_at DESC", (sid,)).fetchone()
    return r["request_id"] if r else None


def _submit(fx, sid, name):
    """참가자가 변경 요청 페이지에서 '일정 변경 요청 보내기' 만 누른다 (처리 방법은 고르지 않는다)."""
    tok = _link(fx, "회의가 확정되었습니다", name)
    r = fx.client.post(f"/change/request/{tok}", data={})
    assert "변경 요청을 보냈어요" in r.text, r.text
    return fx.open_intake_id(sid)


def test_cancel_end_to_end_through_pages_and_background_work():
    with FakeServer() as fx:
        sid = _committed(fx)
        assert fx.events() == 1
        tok = _link(fx, "회의가 확정되었습니다", "도연")

        page = fx.client.get(f"/change/request/{tok}").text
        assert "일정 변경 요청 보내기" in page and fx.events() == 1
        r = fx.client.post(f"/change/request/{tok}", data={})
        assert "변경 요청을 보냈어요" in r.text
        assert fx.events() == 1                                    # 요청만으로는 아무것도 바뀌지 않는다
        assert fx.notifier.mails("일정 변경 요청에 대한 투표") == []   # 주최자가 고르기 전에는 아무도 못 받는다

        dash = fx.client.get(f"/session/{sid}/dashboard").json()
        assert dash["change"]["openIntakes"] == 1
        assert "도연" not in json.dumps(dash["change"], ensure_ascii=False)

        cards = fx.client.get("/api/change-requests").json()["requests"]
        iid = fx.open_intake_id(sid)
        card = next(c for c in cards if c["intakeId"] == iid)
        assert card["requester"] == "도연"                          # 주최자 화면에는 요청자를 보일 수 있다

        resp = fx.decide_change(iid, "cancel")
        assert resp.status_code == 200 and resp.json()["status"] == "voting"
        rid = _open_request(sid)
        assert _state(rid) == "voting" and fx.events() == 1

        assert _wait(lambda: len(fx.notifier.mails("일정 변경 요청에 대한 투표")) == 2)
        voters = {m.to_email for m in fx.notifier.mails("일정 변경 요청에 대한 투표")}
        assert voters == {fx.agent("재원").profile.email, fx.agent("승현").profile.email}
        assert all("도연" not in m.body_text for m in fx.notifier.mails("일정 변경 요청에 대한 투표"))

        dash = fx.client.get(f"/session/{sid}/dashboard").json()
        assert dash["change"]["open"]["state"] == "voting"
        assert dash["change"]["open"]["votes"] == {"responded": 0, "total": 2}
        assert "도연" not in json.dumps(dash["change"], ensure_ascii=False)

        v1 = _link(fx, "일정 변경 요청에 대한 투표", "재원")
        assert "회의를 취소하는 것" in fx.client.get(f"/change/vote/{v1}").text
        assert "다른 참가자의 응답을 기다리는 중" in fx.client.post(
            f"/change/vote/{v1}", data={"verdict": "approve"}).text
        assert fx.events() == 1 and _state(rid) == "voting"

        v2 = _link(fx, "일정 변경 요청에 대한 투표", "승현")
        assert "변경이 승인되었습니다" in fx.client.post(
            f"/change/vote/{v2}", data={"verdict": "approve"}).text

        assert _wait(lambda: fx.events() == 0)                     # 백그라운드에서 이벤트 삭제
        assert _wait(lambda: _state(rid) == "applied")
        assert store.get_session(sid)["state"] == "CANCELLED"
        assert _wait(lambda: len(fx.notifier.mails("일정 변경 결과")) == 3)
        assert all("취소되었습니다" in m.body_text for m in fx.notifier.mails("일정 변경 결과"))
        # 취소된 회의의 링크는 더 이상 쓸 수 없다
        assert "만료되었거나 이미 처리된" in fx.client.get(f"/change/request/{tok}").text


def test_denied_request_leaves_the_meeting_untouched_and_a_new_request_is_possible():
    with FakeServer() as fx:
        sid = _committed(fx)
        iid = _submit(fx, sid, "승현")
        resp = fx.decide_change(iid, "cancel")
        assert resp.json()["status"] == "voting"
        rid = _open_request(sid)
        assert _wait(lambda: len(fx.notifier.mails("일정 변경 요청에 대한 투표")) == 2)
        v = _link(fx, "일정 변경 요청에 대한 투표", "재원")
        assert "받아들여지지 않아" in fx.client.post(f"/change/vote/{v}",
                                                   data={"verdict": "reject"}).text
        assert _state(rid) == "denied" and store.get_session(sid)["state"] == "COMMITTED"
        assert fx.events() == 1
        assert _wait(lambda: len(fx.notifier.mails("일정 변경 결과")) == 3)
        assert all("그대로 유지" in m.body_text for m in fx.notifier.mails("일정 변경 결과"))

        iid2 = _submit(fx, sid, "도연")
        resp2 = fx.decide_change(iid2, "renegotiate")
        assert resp2.json()["status"] == "voting"


def test_renegotiation_end_to_end_moves_the_same_google_event():
    with FakeServer() as fx:
        sid = _committed(fx)
        org = fx.agent("재원")
        ev_id = next(iter(org.backend.created_events))
        old = dict(org.backend.created_events[ev_id])

        iid = _submit(fx, sid, "도연")
        resp = fx.decide_change(iid, "renegotiate")
        assert resp.json()["status"] == "voting"
        rid = _open_request(sid)
        assert _wait(lambda: len(fx.notifier.mails("일정 변경 요청에 대한 투표")) == 2)
        for n in ("재원", "승현"):
            fx.client.post("/change/vote/" + _link(fx, "일정 변경 요청에 대한 투표", n),
                           data={"verdict": "approve"})

        assert _wait(lambda: _state(rid) == "replanning")
        with store.connect() as c:
            s2 = c.execute("SELECT followup_session_id FROM change_requests WHERE request_id = ?",
                           (rid,)).fetchone()["followup_session_id"]
        assert _wait(lambda: len([m for m in fx.notifier.mails("회의 시간 승인 요청")
                                  if m.session_id == s2]) == 3)
        assert org.backend.created_events[ev_id]["start"] == old["start"]     # 아직 그대로

        for n in NAMES:
            m = [x for x in fx.notifier.mails("회의 시간 승인 요청",
                                              fx.agent(n).profile.email) if x.session_id == s2][-1]
            fx.post_link(m, "approve")
        assert _wait(lambda: store.get_session(s2)["state"] == "COMMITTED")
        assert _wait(lambda: _state(rid) == "applied")

        assert len(org.backend.created_events) == 1                            # 이벤트가 하나뿐
        assert org.backend.created_events[ev_id]["start"] != old["start"]
        assert store.get_session(sid)["state"] == "RESCHEDULED"
        assert store.get_session(s2)["google_event_id"] == ev_id
        assert _wait(lambda: len(fx.notifier.mails("일정 변경 결과")) >= 3)
        # 새 확정 안내(새 시간의 변경 링크)가 새 세션 참석자에게 간다
        assert _wait(lambda: len([m for m in fx.notifier.mails("회의가 확정되었습니다")
                                  if m.session_id == s2]) == 3)


def test_failed_calendar_update_is_reported_and_original_meeting_stays():
    with FakeServer() as fx:
        sid = _committed(fx)
        fx.agent("재원").backend.fail_update = True
        iid = _submit(fx, sid, "도연")
        resp = fx.decide_change(iid, "cancel")
        assert resp.json()["status"] == "voting"
        rid = _open_request(sid)
        assert _wait(lambda: len(fx.notifier.mails("일정 변경 요청에 대한 투표")) == 2)
        for n in ("재원", "승현"):
            fx.client.post("/change/vote/" + _link(fx, "일정 변경 요청에 대한 투표", n),
                           data={"verdict": "approve"})
        assert _wait(lambda: _state(rid) == "failed")
        assert store.get_session(sid)["state"] == "COMMITTED" and fx.events() == 1
        assert _wait(lambda: len(fx.notifier.mails("일정 변경 결과")) == 3)
        assert all("그대로 유지" in m.body_text for m in fx.notifier.mails("일정 변경 결과"))
        dash = fx.client.get(f"/session/{sid}/dashboard").json()
        assert dash["change"]["requests"][0]["state"] == "failed"


def test_admin_can_resend_confirmation_links_only_for_committed_meetings():
    with FakeServer() as fx:
        j = fx.client.post("/negotiate", json={}).json()
        assert fx.client.post(f"/session/{j['sessionId']}/change-links/send").status_code == 409
        for n in NAMES:
            fx.post_link(fx.latest_mail(n), "approve")
        sid = j["sessionId"]
        assert _wait(lambda: len(fx.notifier.mails("회의가 확정되었습니다")) == 3)
        r = fx.client.post(f"/session/{sid}/change-links/send")
        assert r.status_code == 200 and r.json()["sent"] == 0 and r.json()["failed"] == 0
        assert fx.client.post("/session/nope/change-links/send").status_code == 404
        # 응답 어디에도 링크가 없다
        assert "/change/" not in r.text


def test_dashboard_and_pages_never_expose_links_or_requester():
    with FakeServer() as fx:
        sid = _committed(fx)
        iid = _submit(fx, sid, "도연")
        fx.decide_change(iid, "cancel")
        assert _wait(lambda: len(fx.notifier.mails("일정 변경 요청에 대한 투표")) == 2)
        blob = fx.client.get(f"/session/{sid}/dashboard").text + fx.client.get("/").text
        for m in fx.notifier.outbox:
            if m.approval_url:
                assert m.approval_url.rsplit("/", 1)[1] not in blob
        assert "/change/" not in blob


def test_organizer_decision_is_required_before_any_vote_mail_goes_out():
    """참가자는 처리 방식을 제출할 수 없다: 접수만으로는 다른 참가자에게 아무 메일도 가지 않는다."""
    with FakeServer() as fx:
        sid = _committed(fx)
        _submit(fx, sid, "도연")
        time.sleep(0.2)                                            # 백그라운드가 있다면 돌 시간을 준다
        assert fx.notifier.mails("일정 변경 요청에 대한 투표") == []
        assert store.get_session(sid)["state"] == "COMMITTED"
        assert fx.events() == 1


def test_decide_endpoint_rejects_unauthenticated_cross_site_requests():
    """관리자 API 는 다른 사이트에서 브라우저가 대신 보낸 요청(Origin 다름)을 받지 않는다."""
    with FakeServer() as fx:
        sid = _committed(fx)
        iid = _submit(fx, sid, "도연")
        r = fx.client.post(f"/api/change-requests/{iid}/decide", json={"action": "cancel"},
                           headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert _open_request(sid) is None                          # 아직 처리되지 않았다
