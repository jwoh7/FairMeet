"""확정된 회의의 일정 변경 (v5): 참가자 접수(intake) → 주최자의 처리 방법 선택(decide) → 투표 → 적용.

참가자는 '일정 변경 요청 보내기'만 한다 (submit_intake, kind 없음). 처리 방법(재협상 / 제외 / 취소 / 거절)은
주최자가 decide_intake 로 고르고, 그때 비로소 다른 참가자에게 투표 메일이 나간다.

실제 이메일(FakeNotifier)·Google Calendar(FakeCalendar)·LLM 은 쓰지 않는다.
시각은 NOW 로 고정해 테스트가 실행 날짜에 좌우되지 않게 한다 (회의는 2026-09-21 월 09:00).
"""
import datetime as dt
import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.slots import KST
from mediator import approval_flow, change_flow, store, tokens
from mediator import commit as commit_mod
from mediator.change_web import router as change_router
from tests.flow_helpers import NAMES, email_of, fresh_db, start_flow

NOW = dt.datetime(2026, 9, 19, 12, 0, tzinfo=KST)          # 회의(9/21 09:00) 이틀 전
QUORUM2 = {"mode": "min_count", "min_count": 2, "required": []}    # 주최자(민지)는 자동 필수


def setup_function(fn=None):
    fresh_db()


# ---- 도우미 -------------------------------------------------------------------------------------

def committed(policy=None, approve=None, send_mail=True):
    """협상 → 승인 → 확정(COMMITTED) 까지 끝낸 세션. 확정 안내 메일도 보낸다.

    정족수 정책이면 기준 인원(주최자+준호)이 승인하는 순간 확정되므로 기본 승인자는 그 둘이다.
    승인하지 않은 서연은 캘린더 초대만 받은 (참석 미확정) 초대 대상이다."""
    if approve is None:
        approve = NAMES if not policy else ("민지", "준호")
    f = start_flow(policy=policy)
    for n in approve:
        f.approve(n)
    assert commit_mod.finalize(f.sid, f.transport).state == "COMMITTED"
    if send_mail:
        assert change_flow.prepare_confirmations(f.sid) >= len(approve)
        change_flow.dispatch_mail(f.sid, "", ("confirm",), notifier=f.notifier, now=NOW)
    return f


def confirm_token(f, name):
    return f.notifier.link_token(f.notifier.mails("회의가 확정되었습니다", email_of(name))[-1])


def request(f, name, now=NOW):
    """참가자가 '일정 변경 요청 보내기'만 한다. 처리 방법은 고르지 않는다."""
    return change_flow.submit_intake(confirm_token(f, name), now=now)


def decide(intake_id, action, now=NOW):
    """주최자가 접수된 요청의 처리 방법을 고른다."""
    return change_flow.decide_intake(intake_id, action, now=now)


def submit_and_decide(f, name, action, now=NOW):
    """참가자 접수 + 주최자의 처리 방법 선택을 한 번에 하는 편의 헬퍼."""
    r = request(f, name, now=now)
    assert r.status == "created", r.message
    return decide(r.intake_id, action, now=now)


def send_votes(f):
    return change_flow.dispatch_mail(f.sid, next(iter(_open_ids(f))), ("vote",),
                                     notifier=f.notifier, now=NOW)


def _open_ids(f):
    with store.connect() as c:
        return [r["request_id"] for r in c.execute(
            "SELECT request_id FROM change_requests WHERE session_id = ? AND state IN "
            "('voting','applying','replanning')", (f.sid,))]


def vote_token(f, name):
    return f.notifier.link_token(f.notifier.mails("일정 변경 요청에 대한 투표",
                                                  email_of(name))[-1])


def vote(f, name, verdict, now=NOW):
    return change_flow.decide_vote(vote_token(f, name), verdict, now=now)


def req_row(rid):
    with store.connect() as c:
        return c.execute("SELECT * FROM change_requests WHERE request_id = ?", (rid,)).fetchone()


def intake_row(iid):
    with store.connect() as c:
        return c.execute("SELECT * FROM change_intakes WHERE intake_id = ?", (iid,)).fetchone()


def organizer_events(f):
    return f.agents[0].backend.created_events


def start_request(f, name, kind):
    """참가자 접수 + 주최자의 선택으로 투표가 열리는 것까지 끝낸다. 기존 헬퍼와 같은 반환값(request_id)."""
    d = submit_and_decide(f, name, kind)
    assert d.status == "created", d.message
    send_votes(f)
    return d.request_id


def apply(f, rid):
    return change_flow.apply_request(rid, f.transport, notifier=f.notifier, now=NOW)


class fixed_now:
    """웹 계층 테스트: change_flow 가 쓰는 현재 시각을 NOW 로 고정한다."""

    def __enter__(self):
        self.orig = change_flow._now
        change_flow._now = lambda now=None: now or NOW
        return self

    def __exit__(self, *a):
        change_flow._now = self.orig


# ---- 확정 안내 메일과 토큰 -------------------------------------------------------------------------

def test_confirmation_mail_carries_a_signed_single_use_link_for_each_attendee():
    f = committed()
    mails = f.notifier.mails("회의가 확정되었습니다")
    assert len(mails) == 3 and len({m.approval_url for m in mails}) == 3
    for name in NAMES:
        m = f.notifier.mails("회의가 확정되었습니다", email_of(name))[-1]
        claims = tokens.verify_change(f.notifier.link_token(m), NOW.timestamp(), purpose="request")
        assert claims.session_id == f.sid and claims.agent_id == f.agent_id(name)
        s, e = f.slots[f.slot_index]
        assert claims.expires_at == int(s.timestamp())              # 회의 시작 시각까지만 유효
        assert "/change/request/" in m.approval_url and "이유를 적을 필요가 없습니다" in m.body_text
        assert "한 번만 사용할 수 있습니다" in m.body_text

    # 이미 보낸 메일은 다시 보내지 않는다
    assert change_flow.dispatch_mail(f.sid, "", ("confirm",), notifier=f.notifier,
                                     now=NOW).sent == 0
    assert len(f.notifier.mails("회의가 확정되었습니다")) == 3


def test_change_tokens_and_approval_tokens_are_not_interchangeable():
    f = committed()
    tok = confirm_token(f, "준호")
    try:
        tokens.verify(tok, NOW.timestamp())
    except tokens.TokenInvalid:
        pass
    else:
        raise AssertionError("변경 토큰이 승인 토큰으로 통과함")
    approval = f.notifier.mails("회의 시간 승인 요청")[0]
    try:
        tokens.verify_change(f.notifier.token_of(approval), NOW.timestamp())
    except tokens.TokenInvalid:
        pass
    else:
        raise AssertionError("승인 토큰이 변경 토큰으로 통과함")
    # 요청용 토큰은 투표에 쓸 수 없다
    assert change_flow.decide_vote(tok, "approve", now=NOW).status == "invalid"
    # 서명이 깨졌거나 만료된 토큰
    assert change_flow.submit_intake(tok[:-3] + "AAA", now=NOW).status == "invalid"
    late = dt.datetime(2026, 9, 21, 9, 30, tzinfo=KST)
    assert change_flow.submit_intake(tok, now=late).status == "invalid"


def test_invitees_who_did_not_confirm_can_only_ask_to_leave_and_decliners_get_no_link():
    f = committed(policy=QUORUM2)                              # 민지·준호가 확정, 서연은 미응답 초대 대상
    assert len(f.notifier.mails("회의가 확정되었습니다")) == 3
    # 서연: 참석을 확정하지 않았으므로 재협상·취소는 주최자가 고를 수 없고, 빠지는 것만 가능하다
    view = change_flow.inspect_request_token(confirm_token(f, "서연"), now=NOW)
    assert view.attends is False and view.can_proceed_without is True
    r = request(f, "서연")
    assert r.status == "created"
    for kind in ("cancel", "renegotiate"):
        d = decide(r.intake_id, kind)
        assert d.status == "rejected" and "고를 수 없" in d.message
    assert _open_ids(f) == []
    assert decide(r.intake_id, "proceed_without").status == "created"

    # 명시적으로 거절한 사람에게는 링크가 가지 않는다
    fresh_db()
    g = start_flow(policy=QUORUM2)
    g.reject("서연")
    g.approve("민지")
    g.approve("준호")
    assert commit_mod.finalize(g.sid, g.transport).state == "COMMITTED"
    change_flow.prepare_confirmations(g.sid)
    change_flow.dispatch_mail(g.sid, "", ("confirm",), notifier=g.notifier, now=NOW)
    assert len(g.notifier.mails("회의가 확정되었습니다")) == 2
    assert g.notifier.mails("회의가 확정되었습니다", email_of("서연")) == []
    tok, jti = tokens.mint_change("request", g.sid, "", g.agent_id("서연"),
                                  int(NOW.timestamp()) + 999999)
    with store.connect() as c:
        c.execute("INSERT INTO change_tokens (jti, purpose, session_id, request_id, agent_id, "
                  "expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
                  (jti, "request", g.sid, "", g.agent_id("서연"), "2999-01-01T00:00:00+00:00",
                   store.now()))
    assert change_flow.submit_intake(tok, now=NOW).status == "invalid"


# ---- 접수 규칙 (참가자는 처리 방법을 고르지 않는다) --------------------------------------------------

def test_a_request_does_not_touch_the_calendar_event_or_the_session():
    f = committed()
    before = json.dumps(organizer_events(f), sort_keys=True, default=str)
    r = request(f, "준호")
    assert r.status == "created"
    assert json.dumps(organizer_events(f), sort_keys=True, default=str) == before
    assert f.session["state"] == "COMMITTED" and f.agents[0].backend.update_calls == 0
    assert _open_ids(f) == []                                   # 처리 방법이 정해지기 전에는 투표도 열리지 않는다
    assert f.notifier.mails("일정 변경 요청에 대한 투표") == []
    row = intake_row(r.intake_id)
    assert row["state"] == "open" and row["decision"] is None


def test_participant_cannot_choose_how_the_change_is_handled():
    """참가자 페이지는 '일정 변경 요청 보내기' 버튼 하나뿐이다. 다른 필드를 보내도 무시된다."""
    f = committed()
    tok = confirm_token(f, "준호")
    c = _client()
    with fixed_now():
        r = c.post(f"/change/request/{tok}", data={"kind": "cancel", "action": "cancel",
                                                    "decision": "renegotiate"})
    assert "변경 요청을 보냈어요" in r.text
    with store.connect() as conn:
        row = conn.execute("SELECT decision, state FROM change_intakes WHERE session_id = ?",
                           (f.sid,)).fetchone()
    assert row["state"] == "open" and row["decision"] is None    # 아무 처리 방법도 선택되지 않았다
    assert _open_ids(f) == [] and f.notifier.mails("일정 변경 요청에 대한 투표") == []


def test_unknown_decisions_are_rejected_and_the_request_link_is_consumed_only_once():
    f = committed()
    tok = confirm_token(f, "준호")
    r = change_flow.submit_intake(tok, now=NOW)
    assert r.status == "created"
    assert change_flow.submit_intake(tok, now=NOW).status == "invalid"      # 링크는 접수 때 소비된다
    assert change_flow.decide_intake(r.intake_id, "explode", now=NOW).status == "rejected"
    assert change_flow.decide_intake(r.intake_id, "", now=NOW).status == "rejected"
    d = change_flow.decide_intake(r.intake_id, "renegotiate", now=NOW)      # 거절돼도 접수는 살아 있었다
    assert d.status == "created"
    assert change_flow.decide_intake(r.intake_id, "cancel", now=NOW).status == "invalid"  # 이제 처리됨


def test_requests_for_past_or_started_meetings_are_refused():
    f = committed()
    tok = confirm_token(f, "준호")
    started = dt.datetime(2026, 9, 21, 9, 5, tzinfo=KST)
    # 링크 만료 전에 회의가 시작된 경우(만료 시각을 늦게 조작한 토큰)도 DB 검사로 막는다
    late_tok, jti = tokens.mint_change("request", f.sid, "", f.agent_id("준호"),
                                       int(started.timestamp()) + 3600)
    with store.connect() as c:
        c.execute("INSERT INTO change_tokens (jti, purpose, session_id, request_id, agent_id, "
                  "expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
                  (jti, "request", f.sid, "", f.agent_id("준호"),
                   "2999-01-01T00:00:00+00:00", store.now()))
    r = change_flow.submit_intake(late_tok, now=started)
    assert r.status == "rejected" and "지난 회의" in r.message
    assert change_flow.inspect_request_token(late_tok, now=started) is None
    assert store.get_session(f.sid)["state"] == "COMMITTED"
    assert _open_ids(f) == []
    assert change_flow.inspect_request_token(tok, now=NOW) is not None


def test_uncommitted_sessions_cannot_be_changed():
    f = start_flow()                                           # 아직 승인 대기
    tok, jti = tokens.mint_change("request", f.sid, "", f.agent_id("준호"),
                                  int(NOW.timestamp()) + 99999)
    with store.connect() as c:
        c.execute("INSERT INTO change_tokens (jti, purpose, session_id, request_id, agent_id, "
                  "expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
                  (jti, "request", f.sid, "", f.agent_id("준호"),
                   "2999-01-01T00:00:00+00:00", store.now()))
    assert change_flow.submit_intake(tok, now=NOW).status in ("rejected", "invalid")
    assert _open_ids(f) == []


# ---- 주최자의 결정 (v5) -----------------------------------------------------------------------------

def test_organizer_can_decline_and_keep_everything_unchanged():
    f = committed()
    r = request(f, "준호")
    d = decide(r.intake_id, "decline")
    assert d.status == "declined"
    assert _open_ids(f) == [] and f.notifier.mails("일정 변경 요청에 대한 투표") == []
    assert f.session["state"] == "COMMITTED" and f.agents[0].backend.update_calls == 0
    change_flow.dispatch_mail(f.sid, r.intake_id, ("result",), notifier=f.notifier, now=NOW)
    results = f.notifier.mails("일정 변경 결과", email_of("준호"))
    assert results and "그대로 유지" in results[0].body_text
    assert f.notifier.mails("일정 변경 결과", email_of("민지")) == []   # 다른 참가자에게는 알리지 않는다
    assert intake_row(r.intake_id)["state"] == "declined"
    assert decide(r.intake_id, "cancel").status == "invalid"     # 이미 처리된 접수는 다시 고를 수 없다


def test_deciding_the_same_intake_twice_only_opens_one_process():
    f = committed()
    r = request(f, "준호")
    out, barrier = [], threading.Barrier(2)

    def go(action):
        barrier.wait()
        out.append(change_flow.decide_intake(r.intake_id, action, now=NOW).status)

    threads = [threading.Thread(target=go, args=(a,)) for a in ("cancel", "decline")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert "invalid" in out and len(_open_ids(f)) <= 1


def test_proceed_without_is_refused_for_all_approve_meetings_and_required_people():
    f = committed()                                            # 전원 승인 회의: 모두 필수
    view = change_flow.inspect_request_token(confirm_token(f, "서연"), now=NOW)
    assert view.can_proceed_without is False and "전원 참석" in view.proceed_without_reason
    d = submit_and_decide(f, "서연", "proceed_without")
    assert d.status == "rejected" and "전원 참석" in d.message

    g = committed(policy=QUORUM2)                              # 주최자는 필수
    d2 = submit_and_decide(g, "민지", "proceed_without")
    assert d2.status == "rejected" and "필수 참석자" in d2.message


def test_proceed_without_requires_the_quorum_to_survive():
    f = committed(policy=QUORUM2)                                     # 확정자 2명 = 기준 2명
    ok = change_flow.inspect_request_token(confirm_token(f, "서연"), now=NOW)
    assert ok.can_proceed_without is True                             # 미응답 초대 대상이 빠지는 것은 무관
    d = submit_and_decide(f, "준호", "proceed_without")                # 확정자가 빠지면 기준 미달
    assert d.status == "rejected" and "정족수" in d.message
    assert _open_ids(f) == []


def test_only_one_open_change_process_per_session_and_a_refusal_lets_the_next_one_start():
    f = committed()
    rid = start_request(f, "준호", "cancel")
    r2 = request(f, "서연")                                     # 서연도 별도로 접수할 수 있다 (막지 않는다)
    assert r2.status == "created"
    d2 = decide(r2.intake_id, "renegotiate")
    assert d2.status == "rejected" and "이미 진행 중" in d2.message
    assert len(_open_ids(f)) == 1

    send_votes(f)
    assert vote(f, "민지", "reject").status == "denied"               # 첫 요청이 끝난다
    d3 = decide(r2.intake_id, "renegotiate")                          # 서연의 접수는 아직 열려 있었다
    assert d3.status == "created"


def test_organizer_deciding_on_several_intakes_at_once_opens_exactly_one_process():
    f = committed()
    intakes = [request(f, n).intake_id for n in NAMES]
    results, barrier = [], threading.Barrier(3)

    def go(iid, kind):
        barrier.wait()
        results.append(change_flow.decide_intake(iid, kind, now=NOW).status)

    threads = [threading.Thread(target=go, args=(iid, k)) for iid, k in
               zip(intakes, ("cancel", "renegotiate", "cancel"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["created", "rejected", "rejected"]
    assert len(_open_ids(f)) == 1


def test_double_submitting_the_same_link_creates_one_intake():
    f = committed()
    tok = confirm_token(f, "준호")
    results, barrier = [], threading.Barrier(2)

    def go():
        barrier.wait()
        results.append(change_flow.submit_intake(tok, now=NOW).status)

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(results) == ["created", "invalid"]
    with store.connect() as c:
        n = c.execute("SELECT COUNT(*) n FROM change_intakes WHERE session_id = ?",
                      (f.sid,)).fetchone()["n"]
    assert n == 1


# ---- 투표 -----------------------------------------------------------------------------------------

def test_vote_mails_go_to_everyone_else_and_reveal_neither_requester_nor_reason():
    f = committed()
    rid = start_request(f, "민지", "cancel")
    mails = f.notifier.mails("일정 변경 요청에 대한 투표")
    assert sorted(m.to_email for m in mails) == sorted([email_of("준호"), email_of("서연")])
    for m in mails:
        assert "/change/vote/" in m.approval_url and "회의 취소" in m.body_text
        assert "민지" not in m.body_text and "민지" not in m.body_html      # 요청자 신원
        assert "사유" in m.body_text and "공개되지 않습니다" in m.body_text
        claims = tokens.verify_change(f.notifier.link_token(m), NOW.timestamp(), purpose="vote")
        assert claims.request_id == rid and claims.session_id == f.sid
    assert change_flow.dispatch_mail(f.sid, rid, ("vote",), notifier=f.notifier, now=NOW).sent == 0


def test_all_approve_meeting_needs_every_other_attendee_to_agree():
    f = committed()
    rid = start_request(f, "민지", "cancel")
    assert vote(f, "준호", "approve").status == "recorded"
    assert req_row(rid)["state"] == "voting"
    assert vote(f, "서연", "approve").status == "accepted"
    assert req_row(rid)["state"] == "applying"

    g = committed()
    rid = start_request(g, "민지", "cancel")
    res = vote(g, "준호", "reject")
    assert res.status == "denied" and req_row(rid)["state"] == "denied"
    assert g.session["state"] == "COMMITTED" and len(organizer_events(g)) == 1


def test_vote_links_are_single_use_and_bound_to_the_voter():
    f = committed()
    rid = start_request(f, "민지", "cancel")
    tok = vote_token(f, "준호")
    assert change_flow.decide_vote(tok, "approve", now=NOW).status == "recorded"
    assert change_flow.decide_vote(tok, "reject", now=NOW).status == "invalid"     # 재사용
    assert change_flow.inspect_vote_token(tok, now=NOW) is None
    # 다른 사람의 링크로 다른 사람인 척할 수 없다: 토큰에 참가자가 들어 있다
    claims = tokens.verify_change(tok, NOW.timestamp())
    assert claims.agent_id == f.agent_id("준호")
    # 요청자에게는 투표 링크가 없다
    assert f.notifier.mails("일정 변경 요청에 대한 투표", email_of("민지")) == []
    forged, jti = tokens.mint_change("vote", f.sid, rid, f.agent_id("민지"),
                                     int(NOW.timestamp()) + 999)
    assert change_flow.decide_vote(forged, "approve", now=NOW).status == "invalid"
    # 잘못된 verdict
    try:
        change_flow.decide_vote(vote_token(f, "서연"), "maybe", now=NOW)
    except ValueError:
        pass
    else:
        raise AssertionError("잘못된 verdict 가 통과함")


def test_quorum_meeting_uses_the_original_rule_and_early_decisions():
    # 기준 2명(주최자 필수). 확정자는 민지·준호이고 서연은 참석 미확정 초대 대상이다.
    f = committed(policy=QUORUM2)
    rid = start_request(f, "준호", "cancel")                   # 준호(선택)가 취소를 요청
    assert f.notifier.mails("일정 변경 요청에 대한 투표", email_of("서연")) == []    # 유권자가 아니다
    assert vote(f, "민지", "approve").status == "accepted"     # 요청자 + 필수(주최자) = 기준 충족

    g = committed(policy=QUORUM2)
    rid = start_request(g, "준호", "cancel")
    assert vote(g, "민지", "reject").status == "denied"        # 필수 참석자의 거절은 즉시 부결
    assert g.session["state"] == "COMMITTED"

    h = committed(policy=QUORUM2)
    rid = start_request(h, "민지", "cancel")                   # 주최자 요청
    assert vote(h, "준호", "approve").status == "accepted"     # 요청자 + 준호 = 기준 충족

    i = committed(policy=QUORUM2)
    rid = start_request(i, "민지", "renegotiate")
    assert vote(i, "준호", "reject").status == "denied"        # 기준(2명)을 채울 수 없다


def test_expired_requests_end_without_changing_anything_and_links_die():
    f = committed()
    rid = start_request(f, "준호", "cancel")
    assert vote(f, "민지", "approve").status == "recorded"
    tok = vote_token(f, "서연")
    row = req_row(rid)
    later = dt.datetime.fromisoformat(row["expires_at"]) + dt.timedelta(minutes=1)
    assert change_flow.decide_vote(tok, "approve", now=later).status == "invalid"
    assert change_flow.expire_stale_changes(now=later) == [rid]
    assert req_row(rid)["state"] == "expired" and f.session["state"] == "COMMITTED"
    assert len(organizer_events(f)) == 1
    assert change_flow.decide_vote(tok, "approve", now=NOW).status == "invalid"   # 요청이 끝났다
    assert change_flow.expire_stale_changes(now=later) == []                       # 멱등
    # 결과 안내가 대기열에 들어가고, 다음 요청이 가능하다
    change_flow.dispatch_mail(f.sid, rid, ("result",), notifier=f.notifier, now=NOW)
    assert any("시간이 지나" in m.body_text for m in f.notifier.mails("일정 변경 결과"))
    assert submit_and_decide(f, "서연", "cancel").status == "created"


def test_two_voters_finishing_at_the_same_moment_accept_only_once():
    f = committed()
    rid = start_request(f, "민지", "cancel")
    toks = [vote_token(f, "준호"), vote_token(f, "서연")]
    out, barrier = [], threading.Barrier(2)

    def go(t):
        barrier.wait()
        out.append(change_flow.decide_vote(t, "approve", now=NOW).status)

    ts = [threading.Thread(target=go, args=(t,)) for t in toks]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(out) == ["accepted", "recorded"]
    assert req_row(rid)["state"] == "applying"


# ---- 적용: 취소 -------------------------------------------------------------------------------------

def test_cancel_is_applied_only_after_the_vote_and_only_once():
    f = committed()
    rid = start_request(f, "준호", "cancel")
    vote(f, "민지", "approve")
    assert len(organizer_events(f)) == 1                       # 투표 중에는 이벤트가 그대로
    vote(f, "서연", "approve")
    assert len(organizer_events(f)) == 1                       # 통과 직후에도 아직 적용 전
    assert apply(f, rid) == "applied"
    assert organizer_events(f) == {} and len(f.agents[0].backend.cancelled_events) == 1
    assert f.session["state"] == "CANCELLED" and req_row(rid)["state"] == "applied"
    assert apply(f, rid) == "applied"                          # 다시 불러도 아무 일도 없다
    assert len(f.agents[0].backend.cancelled_events) == 1

    change_flow.dispatch_mail(f.sid, rid, ("result",), notifier=f.notifier, now=NOW)
    results = f.notifier.mails("일정 변경 결과")
    assert len(results) == 3 and all("취소되었습니다" in m.body_text for m in results)
    assert all("준호" not in m.body_text.replace("준호 님", "") for m in results)


def test_cancel_treats_an_already_deleted_event_as_success():
    f = committed()
    rid = start_request(f, "준호", "cancel")
    vote(f, "민지", "approve")
    vote(f, "서연", "approve")
    f.agents[0].backend.cancel_event(next(iter(organizer_events(f))))    # 누군가 이미 지웠다
    assert apply(f, rid) == "applied" and f.session["state"] == "CANCELLED"


def test_failed_cancel_keeps_the_original_meeting_and_allows_a_new_request():
    f = committed()
    rid = start_request(f, "준호", "cancel")
    vote(f, "민지", "approve")
    vote(f, "서연", "approve")
    f.agents[0].backend.fail_update = True
    assert apply(f, rid) == "failed"
    assert f.session["state"] == "COMMITTED" and len(organizer_events(f)) == 1
    assert "주입" in (req_row(rid)["fail_reason"] or "") or req_row(rid)["fail_reason"]
    change_flow.dispatch_mail(f.sid, rid, ("result",), notifier=f.notifier, now=NOW)
    assert all("기존 일정이 그대로 유지" in m.body_text for m in f.notifier.mails("일정 변경 결과"))

    f.agents[0].backend.fail_update = False
    assert submit_and_decide(f, "서연", "cancel").status == "created"     # 실패한 요청은 더는 진행 중이 아니다


def test_a_stuck_application_is_retried_safely_after_a_crash():
    f = committed()
    rid = start_request(f, "준호", "cancel")
    vote(f, "민지", "approve")
    vote(f, "서연", "approve")
    with store.connect() as c:                                 # 적용 도중 서버가 멈춘 상황
        c.execute("UPDATE change_requests SET updated_at = '2000-01-01T00:00:00+00:00' "
                  "WHERE request_id = ?", (rid,))
    assert change_flow.resume_stuck_applications(f.transport, notifier=f.notifier, now=NOW) == [rid]
    assert req_row(rid)["state"] == "applied" and f.session["state"] == "CANCELLED"
    assert change_flow.resume_stuck_applications(f.transport, notifier=f.notifier, now=NOW) == []


# ---- 적용: 요청자 제외 진행 -----------------------------------------------------------------------------

def test_proceed_without_updates_the_same_event_and_keeps_everything_else():
    f = committed(policy=QUORUM2)
    rid = start_request(f, "서연", "proceed_without")
    assert vote(f, "준호", "approve").status == "recorded"     # 필수는 주최자
    assert vote(f, "민지", "approve").status == "accepted"
    ev_id = next(iter(organizer_events(f)))
    assert apply(f, rid) == "applied"
    ev = organizer_events(f)[ev_id]
    assert ev["attendees"] == [email_of("준호")]               # 서연만 빠진다 (주최자는 이벤트 소유자)
    assert len(organizer_events(f)) == 1 and f.session["state"] == "COMMITTED"
    assert f.agents[0].backend.update_calls == 1

    # 빠진 사람은 이후 변경 투표 대상이 아니고, 더 빠지면 기준이 깨진다
    d = submit_and_decide(f, "준호", "proceed_without")
    assert d.status == "rejected"


def test_failed_proceed_without_leaves_the_invite_list_untouched():
    f = committed(policy=QUORUM2)
    rid = start_request(f, "서연", "proceed_without")
    vote(f, "준호", "approve")
    vote(f, "민지", "approve")
    ev_id = next(iter(organizer_events(f)))
    before = list(organizer_events(f)[ev_id]["attendees"])
    f.agents[0].backend.fail_update = True
    assert apply(f, rid) == "failed"
    assert organizer_events(f)[ev_id]["attendees"] == before


# ---- 적용: 재협상 ------------------------------------------------------------------------------------

def test_renegotiation_moves_the_same_event_after_everyone_agrees_again():
    f = committed()
    ev_id = next(iter(organizer_events(f)))
    old = dict(organizer_events(f)[ev_id])
    rid = start_request(f, "준호", "renegotiate")
    vote(f, "민지", "approve")
    assert vote(f, "서연", "approve").status == "accepted"
    assert organizer_events(f)[ev_id]["start"] == old["start"]        # 재협상 통과만으로는 안 바뀐다
    assert apply(f, rid) == "replanning"

    row = req_row(rid)
    s2 = row["followup_session_id"]
    child = store.get_session(s2)
    assert child["parent_session_id"] == f.sid and child["change_request_id"] == rid
    assert child["approval_policy_json"] == f.session["approval_policy_json"]
    assert child["state"] == "PENDING_HUMAN_APPROVAL"
    s, e = store.get_slots(s2)[child["proposed_slot"]]
    o_s, o_e = dt.datetime.fromisoformat(old["start"]), dt.datetime.fromisoformat(old["end"])
    assert not (s < o_e and o_s < e)                                  # 원래 시간은 다시 제안하지 않는다
    assert f.session["state"] == "COMMITTED"

    # 새 제안은 기존과 같은 승인 흐름(이메일 링크)으로 전원 승인해야 한다
    mails = [m for m in f.notifier.mails("회의 시간 승인 요청") if m.session_id == s2]
    assert len(mails) == 3 and all("재협상" in m.body_text for m in mails)
    for m in mails:
        res = approval_flow.decide_with_token(f.notifier.token_of(m), "approve")
    assert res.status == "all_approved"
    assert commit_mod.finalize(s2, f.transport).state == "COMMITTED"

    assert len(organizer_events(f)) == 1                              # 이벤트가 새로 생기지 않았다
    moved = organizer_events(f)[ev_id]
    assert moved["start"] == s.isoformat() and moved["end"] == e.isoformat()
    assert f.agents[0].backend.update_calls == 1
    assert store.get_session(s2)["google_event_id"] == ev_id
    assert f.session["state"] == "RESCHEDULED" and req_row(rid)["state"] == "applied"
    # 새 일정에서도 다시 변경을 요청할 수 있다 (새 세션이 이제 확정된 회의)
    assert change_flow.prepare_confirmations(s2) == 3


def test_failed_or_rejected_renegotiation_keeps_the_original_schedule():
    f = committed()
    ev_id = next(iter(organizer_events(f)))
    old = dict(organizer_events(f)[ev_id])
    rid = start_request(f, "준호", "renegotiate")
    vote(f, "민지", "approve")
    vote(f, "서연", "approve")
    assert apply(f, rid) == "replanning"
    s2 = req_row(rid)["followup_session_id"]

    # 후보를 계속 거절해 소진시킨다
    for _ in range(6):
        res = approval_flow.decide_for_agent(s2, f.agent_id("서연"), "reject")
        if res.status == "terminated":
            break
        if res.status == "reopened":
            approval_flow.dispatch_notifications(s2, f.notifier)
    assert store.get_session(s2)["state"] in store.TERMINAL_STATES
    assert req_row(rid)["state"] == "failed"
    assert organizer_events(f)[ev_id] == old and f.session["state"] == "COMMITTED"
    assert f.agents[0].backend.update_calls == 0

    # 요청자는 다시 요청할 수 있다
    assert submit_and_decide(f, "서연", "renegotiate").status == "created"


def test_a_failure_while_moving_the_event_leaves_the_original_time():
    f = committed()
    ev_id = next(iter(organizer_events(f)))
    old = dict(organizer_events(f)[ev_id])
    rid = start_request(f, "준호", "renegotiate")
    vote(f, "민지", "approve")
    vote(f, "서연", "approve")
    apply(f, rid)
    s2 = req_row(rid)["followup_session_id"]
    for n in NAMES:
        m = [x for x in f.notifier.mails("회의 시간 승인 요청", email_of(n))
             if x.session_id == s2][-1]
        approval_flow.decide_with_token(f.notifier.token_of(m), "approve")

    f.agents[0].backend.fail_update = True
    res = commit_mod.finalize(s2, f.transport)
    assert res.state == "COMMIT_FAILED"
    assert organizer_events(f)[ev_id] == old                          # 기존 시간 그대로
    assert f.session["state"] == "COMMITTED" and req_row(rid)["state"] == "failed"
    assert store.get_session(s2)["google_event_id"] is None
    assert len(organizer_events(f)) == 1


def test_renegotiation_inherits_the_quorum_policy():
    f = committed(policy=QUORUM2, approve=("민지", "준호"))
    rid = start_request(f, "준호", "renegotiate")
    assert vote(f, "민지", "approve").status == "accepted"           # 요청자 + 필수 = 기준 충족
    apply(f, rid)
    s2 = store.get_session(req_row(rid)["followup_session_id"])
    assert s2["approvals_needed"] == 2 and s2["approval_policy_json"] == f.session[
        "approval_policy_json"]
    assert json.loads(s2["required_agents_json"]) == json.loads(f.session["required_agents_json"])


# ---- 조율 / 화면용 뷰 --------------------------------------------------------------------------------

def test_dashboard_view_shows_progress_but_not_who_requested_or_any_link():
    f = committed()
    rid = start_request(f, "준호", "renegotiate")
    vote(f, "민지", "approve")
    v = change_flow.change_view(f.sid)
    assert v["open"]["kind"] == "renegotiate" and v["open"]["state"] == "voting"
    assert v["open"]["votes"] == {"responded": 1, "total": 2}
    assert v["confirmationMails"] == {"sent": 3}
    blob = json.dumps(v, ensure_ascii=False)
    for secret in (f.agent_id("준호"), "준호", "/change/", "requester") + tuple(
            confirm_token(f, n) for n in NAMES):
        assert secret not in blob, secret
    assert change_flow.change_view("nope") is None
    legacy = start_flow()
    assert change_flow.change_view(legacy.sid) is None


def test_organizer_card_shows_the_requester_and_reasons_but_dashboard_view_does_not():
    """주최자에게는 처리에 필요한 범위에서 요청자를 보여줄 수 있다 (intake_cards).
    반면 change_view(기본 대시보드 뷰)는 여전히 요청자를 드러내지 않는다."""
    f = committed()
    r = request(f, "준호")
    cards = change_flow.intake_cards(f.sid)
    assert len(cards) == 1 and cards[0]["requester"] == "준호"
    by_action = {o["action"]: o for o in cards[0]["options"]}
    assert by_action["decline"]["enabled"] is True
    v = change_flow.change_view(f.sid)
    assert "준호" not in json.dumps(v, ensure_ascii=False)
    assert v["intakes"][0]["intakeId"] == r.intake_id and v["openIntakes"] == 1


def test_reconcile_all_settles_a_replanning_request_from_the_new_session_state():
    f = committed()
    rid = start_request(f, "준호", "renegotiate")
    vote(f, "민지", "approve")
    vote(f, "서연", "approve")
    apply(f, rid)
    s2 = req_row(rid)["followup_session_id"]
    assert change_flow.reconcile_all() == []                           # 아직 진행 중
    with store.connect() as c:
        c.execute("UPDATE sessions SET state = 'EXPIRED' WHERE session_id = ?", (s2,))
    assert change_flow.reconcile_all() == [rid]
    assert req_row(rid)["state"] == "failed" and f.session["state"] == "COMMITTED"


def test_mail_failures_are_recorded_and_can_be_retried():
    f = committed()
    start_request(f, "준호", "cancel")
    f.notifier.fail_for = {email_of("서연")}
    rid = _open_ids(f)[0]
    with store.connect() as c:
        c.execute("UPDATE change_mail SET status = 'pending' WHERE request_id = ?", (rid,))
    out = change_flow.dispatch_mail(f.sid, rid, ("vote",), notifier=f.notifier, now=NOW)
    assert out.failed >= 1 and out.errors
    f.notifier.fail_for = set()
    again = change_flow.dispatch_mail(f.sid, rid, ("vote",), notifier=f.notifier, now=NOW)
    assert again.failed == 0 and again.sent >= 1


# ---- 웹 페이지 ---------------------------------------------------------------------------------------------

def _client():
    app = FastAPI()
    app.include_router(change_router)
    return TestClient(app)


def test_change_request_page_offers_only_one_button_and_hides_other_identities():
    f = committed()
    c = _client()
    with fixed_now():
        tok = confirm_token(f, "준호")
        for _ in range(2):
            r = c.get(f"/change/request/{tok}")
            assert r.status_code == 200 and "일정 변경 요청 보내기" in r.text
        assert r.headers["cache-control"] == "no-store" and "textarea" not in r.text.lower()
        # 처리 방법을 고르는 요소가 없다: 참가자는 제출 버튼 하나만 본다
        for forbidden in ("재협상", "제외하고", "취소 제안", "radio", "이유를 설명"):
            assert forbidden not in r.text
        assert "서연" not in r.text and "민지" not in r.text            # 다른 참가자 정보 없음

        r = c.post(f"/change/request/{tok}", data={"kind": "cancel"})     # kind 는 무시된다
        assert "변경 요청을 보냈어요" in r.text
        assert "만료되었거나 이미 처리된" in c.post(f"/change/request/{tok}",
                                                    data={"kind": "cancel"}).text
        assert "만료되었거나 이미 처리된" in c.get(f"/change/request/{tok}").text

        with store.connect() as conn:
            iid = conn.execute("SELECT intake_id FROM change_intakes WHERE session_id = ?",
                               (f.sid,)).fetchone()["intake_id"]
        d = change_flow.decide_intake(iid, "cancel", now=NOW)
        assert d.status == "created"
        send_votes(f)
        vt = vote_token(f, "서연")
        page = c.get(f"/change/vote/{vt}")
        assert "회의를 취소하는 것" in page.text and "준호" not in page.text
        assert "요청한 분이 누구인지" in page.text
        r = c.post(f"/change/vote/{vt}", data={"verdict": "reject"})
        assert "받아들여지지 않아" in r.text and f.session["state"] == "COMMITTED"
        assert "만료되었거나 이미 처리된" in c.post(f"/change/vote/{vt}",
                                                    data={"verdict": "approve"}).text
        for bad in ("/change/vote/garbage", "/change/request/garbage.garbage"):
            assert "만료되었거나 이미 처리된" in c.get(bad).text
        assert c.post(f"/change/vote/{vote_token(f, '민지')}",
                      data={"verdict": "maybe"}).status_code == 200      # 무효 verdict 도 소비하지 않는다


def test_participant_page_hides_option_reasons_but_the_organizer_card_shows_them():
    f = committed()                                            # 전원 승인 회의
    c = _client()
    with fixed_now():
        tok = confirm_token(f, "서연")
        page = c.get(f"/change/request/{tok}").text
        for hidden in ("선택할 수 없습니다", "전원 참석", "정족수", "필수 참석자"):
            assert hidden not in page
        r = c.post(f"/change/request/{tok}", data={})
        assert "변경 요청을 보냈어요" in r.text
    with store.connect() as conn:
        iid = conn.execute("SELECT intake_id FROM change_intakes WHERE session_id = ?",
                           (f.sid,)).fetchone()["intake_id"]
    cards = change_flow.intake_cards(f.sid)
    opt = next(o for o in cards[0]["options"] if o["action"] == "proceed_without")
    assert opt["enabled"] is False and "전원 참석" in opt["reason"]
    assert cards[0]["requester"] == "서연"
    d = change_flow.decide_intake(iid, "cancel", now=NOW)
    assert d.status == "created"
