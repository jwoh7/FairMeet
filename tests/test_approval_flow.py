"""거절 → 차순위 재제안 → 종료 처리 테스트.

실제 이메일/실제 Google Calendar 없이 FakeNotifier + FakeCalendar 로 돌린다.
"""
import json
import sqlite3
import threading

from common import config
from common.fairness import solve, update_balance
from mediator import approval_flow, store
from mediator import commit as commit_mod
from mediator.notifier import FakeNotifier
from tests.flow_helpers import (GID, NAMES, MaxRounds, blk, email_of, fresh_db,
                                make_agent, start_flow)

PENDING = "PENDING_HUMAN_APPROVAL"


def setup_function(fn=None):
    fresh_db()


def _expected_next_slot(flow) -> int:
    """저장된 regret 행렬·현재 잔액·같은 lam 으로 solve() 한 결과 — 재제안이 따라야 할 정답."""
    regret, _, _ = store.load_regret(flow.sid)
    n = len(next(iter(regret.values())))
    common = {i for i in range(n) if all(regret[a][i] is not None for a in regret)}
    balance = store.get_balance(GID, list(regret))
    res = solve(regret, balance, common - store.get_attempted_slots(flow.sid),
                lam=config.LAMBDA_WORK)
    return res.slot_index


def _race(fns):
    """모든 함수를 같은 순간에 시작시킨다."""
    barrier = threading.Barrier(len(fns))
    results, errors = [None] * len(fns), []

    def run(i, fn):
        try:
            barrier.wait(timeout=10)
            results[i] = fn()
        except Exception as exc:                       # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    return results


# ---- 1순위 거절 → 2순위 제안 ---------------------------------------------------

def test_first_rejection_proposes_next_best_slot():
    f = start_flow()
    first = f.slot_index
    expected = _expected_next_slot(f)          # v1 이 이미 '시도함'이므로 그 다음 최선
    res = f.reject("준호")

    assert res.status == "reopened" and res.proposal_version == 2
    assert f.session["state"] == PENDING
    assert f.version == 2
    assert f.slot_index == expected and expected != first
    assert [(p["version"], p["status"]) for p in store.get_proposals(f.sid)] == \
        [(1, "rejected"), (2, "open")]
    assert store.get_attempted_slots(f.sid) == {first, expected}


def _agents_with_different_load():
    """일정이 몰린 요일이 서로 다른 세 사람 (개인 취향 점수는 없다): 저녁 19~22시의 고정 일정이 사람마다 다른 요일에 있다."""
    return [make_agent("민지", True, busy={"primary": [blk(d, 19, 22) for d in (0, 1)]}),
            make_agent("준호"),
            make_agent("서연", busy={"primary": [blk(d, 19, 22) for d in (3, 4)]})]


def test_reproposal_uses_same_objective_lambda_and_current_balance():
    """재제안은 처음 협상과 같은 solve()·같은 lam·현재 양보 잔액을 쓴다."""
    agents = _agents_with_different_load()
    store.seed_balance(GID, {agents[0].agent_id: 30.0, agents[1].agent_id: -15.0,
                             agents[2].agent_id: -15.0})
    f = start_flow(agents=agents)
    assert f.session["lam"] == config.LAMBDA_WORK
    expected = _expected_next_slot(f)
    assert f.reject("준호").status == "reopened"
    assert f.slot_index == expected


def test_reproposal_sends_new_email_with_new_slot_to_everyone():
    f = start_flow()
    first_slot_text = f.notifier.outbox[0].subject
    assert len(f.notifier.outbox) == 3
    f.reject_and_notify("준호")

    assert len(f.notifier.outbox) == 6
    second = [m for m in f.notifier.outbox if m.proposal_version == 2]
    assert {m.to_email for m in second} == {email_of(n) for n in NAMES}
    assert all(m.subject != first_slot_text for m in second)      # 새 시간
    assert all("대체" in m.body_text for m in second)             # 이전 제안을 대체한다는 안내


def test_new_proposal_requires_all_participants_to_approve_again():
    f = start_flow()
    assert f.approve("민지").status == "recorded"
    assert f.approve("서연").status == "recorded"
    assert f.reject_and_notify("준호").status == "reopened"

    assert store.approval_tally(f.sid) == (0, 0, 3)               # 이전 승인은 세지 않는다
    with store.connect() as c:                                    # 감사용으로 남아는 있다
        n_v1 = c.execute("SELECT COUNT(*) n FROM approvals WHERE session_id=? "
                         "AND proposal_version=1", (f.sid,)).fetchone()["n"]
    assert n_v1 == 3

    assert f.approve("민지").status == "recorded"
    assert f.approve("서연").status == "recorded"
    assert f.session["state"] == PENDING                          # 아직 확정 아님
    assert f.approve("준호").status == "all_approved"


def test_previous_links_are_invalidated():
    f = start_flow()
    old = {n: f.token_for(n) for n in NAMES}
    f.reject_and_notify("준호")
    new = {n: f.token_for(n) for n in NAMES}

    assert all(old[n] != new[n] for n in NAMES)
    assert len(set(new.values())) == 3                            # 참가자마다 다른 링크
    assert approval_flow.inspect_token(old["민지"]) is None
    assert approval_flow.inspect_token(new["민지"]) is not None
    assert f.approve("민지", token=old["민지"]).status == "invalid"
    assert f.reject("서연", token=old["서연"]).status == "invalid"
    assert store.approval_tally(f.sid) == (0, 0, 3)
    assert f.version == 2                                          # 낡은 링크가 상태를 안 바꿈
    with store.connect() as c:
        live = c.execute("SELECT COUNT(*) n FROM approval_tokens WHERE "
                         "proposal_version=1 AND used_at IS NULL AND revoked_at IS NULL"
                         ).fetchone()["n"]
    assert live == 0


def test_same_slot_is_never_proposed_twice():
    with MaxRounds(10):
        f = start_flow(nslots=6)
        seen = [f.slot_index]
        while True:
            res = f.reject_and_notify("준호")
            if res.status != "reopened":
                break
            seen.append(f.slot_index)
    assert len(seen) == len(set(seen)) == 6                        # 6개 후보를 각각 정확히 1번
    assert res.state == "CANDIDATES_EXHAUSTED"


def test_database_refuses_duplicate_slot_proposal():
    f = start_flow()
    already_tried = f.slot_index          # 트랜잭션 밖에서 읽는다 (안에서 새 연결을 열면 락에 걸린다)
    try:
        with store.connect() as c:
            store._open_proposal_tx(c, f.sid, already_tried, {})
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("이미 시도한 슬롯의 재제안이 DB 에서 거부되지 않음")
    assert f.version == 1                                          # 롤백됨


def test_rejection_stores_no_reason_and_broadcasts_no_identity():
    cols = {r[1] for r in sqlite3.connect(store._db_path).execute(
        "PRAGMA table_info(approvals)")}
    assert not cols & {"reason", "comment", "note", "detail"}

    events = []
    approval_flow.configure(emit=events.append)
    try:
        f = start_flow()
        f.reject_and_notify("준호")
    finally:
        approval_flow.reset_hooks()
    rejected = [e for e in events if e["type"] == "proposal_rejected"]
    assert rejected and set(rejected[0]) == {"type", "round"}
    blob = json.dumps(events, ensure_ascii=False)
    assert "준호" not in blob and f.agent_id("준호") not in blob


# ---- 종료 처리 ----------------------------------------------------------------

def test_all_candidates_rejected_ends_with_candidates_exhausted():
    with MaxRounds(10):
        f = start_flow(nslots=3)
        for _ in range(3):
            res = f.reject_and_notify("준호")

    assert res.status == "terminated" and res.state == "CANDIDATES_EXHAUSTED"
    s = f.session
    assert s["state"] == "CANDIDATES_EXHAUSTED" and s["fail_reason"]
    assert len(store.get_proposals(f.sid)) == 3
    assert len(f.notifier.outbox) == 9                             # 3개 제안 × 3명, 그 이상 없음

    assert f.dispatch().attempted == 0                             # 종료 후에는 발송 없음
    assert len(f.notifier.outbox) == 9
    assert f.approve("민지").status == "invalid"                   # 마지막 링크도 무효
    assert f.created_events() == 0
    assert all(abs(v) < 1e-9 for v in f.balances().values())
    assert store.get_history(GID) == []
    assert s["google_event_id"] is None

    view = approval_flow.session_view(f.sid)
    assert view["terminal"] and view["state"] == "CANDIDATES_EXHAUSTED"
    assert [o["action"] for o in view["outcome"]["options"]] == \
        ["widen_range", "shorten_duration", "close"]


def test_max_approval_rounds_stops_the_loop():
    with MaxRounds(2):
        f = start_flow(nslots=6)
        assert f.reject_and_notify("준호").status == "reopened"
        res = f.reject_and_notify("준호")

    assert res.state == "MAX_APPROVAL_ROUNDS_REACHED"
    assert f.session["state"] == "MAX_APPROVAL_ROUNDS_REACHED"
    assert len(store.get_proposals(f.sid)) == 2
    assert len(f.notifier.outbox) == 6
    assert f.created_events() == 0 and store.get_history(GID) == []
    assert f.dispatch().attempted == 0


def test_rounds_are_bounded_by_min_of_candidates_and_max_rounds():
    cases = [(2, 5, "CANDIDATES_EXHAUSTED"), (5, 2, "MAX_APPROVAL_ROUNDS_REACHED"),
             (3, 3, "CANDIDATES_EXHAUSTED"), (4, 1, "MAX_APPROVAL_ROUNDS_REACHED")]
    for nslots, max_rounds, state in cases:
        fresh_db()
        with MaxRounds(max_rounds):
            f = start_flow(nslots=nslots)
            guard = 0
            while f.session["state"] == PENDING:
                f.reject_and_notify("준호")
                guard += 1
                assert guard <= 20, "무한 반복"
        n = len(store.get_proposals(f.sid))
        assert n == min(nslots, max_rounds), (nslots, max_rounds, n)
        assert f.session["state"] == state, (nslots, max_rounds)


def test_no_common_slot_ends_immediately_without_asking_anyone():
    # 각자 가능한 시간은 있지만 겹치지 않는다: 민지는 월요일만, 준호는 화요일만
    a = make_agent("민지", organizer=True,
                   busy={"care": [blk(d, 8, 20) for d in (1, 2, 3, 4)]})
    b = make_agent("준호", busy={"care": [blk(d, 8, 20) for d in (0, 2, 3, 4)]})
    f = start_flow(agents=[a, b])

    assert f.outcome.state == "NO_COMMON_SLOT"
    assert f.session["state"] == "NO_COMMON_SLOT" and f.session["fail_reason"]
    assert store.get_proposals(f.sid) == []
    assert store.get_notifications(f.sid) == []
    assert f.notifier.outbox == [] and f.dispatch().attempted == 0
    assert f.created_events() == 0

    view = approval_flow.session_view(f.sid)
    assert view["terminal"] and view["timelineText"] == "공통 시간 없음"
    assert [o["label"] for o in view["outcome"]["options"]] == [
        "날짜 범위를 넓혀 새 협상 시작", "회의 시간을 줄여 새 협상 시작", "이번 협상 종료"]


def test_expired_session_sends_nothing_and_links_die():
    f = start_flow()
    with store.connect() as c:
        c.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00' "
                  "WHERE session_id=?", (f.sid,))
    assert f.approve("민지").status == "invalid"
    assert f.sid in store.expire_stale_sessions()
    assert f.session["state"] == "EXPIRED"
    assert store.get_proposals(f.sid)[0]["status"] == "expired"
    sent = len(f.notifier.outbox)
    assert f.dispatch().attempted == 0 and len(f.notifier.outbox) == sent


# ---- 멱등성 / 경쟁 상태 --------------------------------------------------------

def test_duplicate_requests_are_idempotent():
    f = start_flow()
    tok = f.token_for("민지")
    assert f.approve("민지").status == "recorded"
    for _ in range(3):                                             # 새로고침·재전송
        assert f.approve("민지", token=tok).status == "invalid"
    assert store.approval_tally(f.sid) == (1, 0, 3)
    # 같은 사람이 마음을 바꿔 다른 값을 보내도 첫 응답이 유지된다
    assert approval_flow.decide_for_agent(f.sid, f.agent_id("민지"), "reject").status == "invalid"
    assert f.version == 1

    tokc = f.token_for("준호")
    assert f.reject("준호", token=tokc).status == "reopened"
    assert f.reject("준호", token=tokc).status == "invalid"        # 두 번째는 넘어가지 않는다
    assert f.version == 2 and len(store.get_proposals(f.sid)) == 2

    f.dispatch()
    n = len(f.notifier.outbox)
    f.dispatch()                                                   # 재호출해도 이미 발송된 건 다시 안 보냄
    assert len(f.notifier.outbox) == n


def test_concurrent_approve_and_reject_never_corrupt_state():
    for _ in range(8):
        fresh_db()
        f = start_flow()
        ta, tc = f.token_for("민지"), f.token_for("서연")
        ra, rc = _race([lambda: approval_flow.decide_with_token(ta, "approve"),
                        lambda: approval_flow.decide_with_token(tc, "reject")])
        assert rc.status == "reopened"                              # 거절은 어느 순서든 반영
        assert ra.status in ("recorded", "invalid")                 # 승인은 순서에 따라 기록/무효
        s = f.session
        assert s["state"] == PENDING and s["proposal_version"] == 2
        assert [p["status"] for p in store.get_proposals(f.sid)] == ["rejected", "open"]
        assert store.approval_tally(f.sid) == (0, 0, 3)
        assert f.created_events() == 0


def test_two_simultaneous_rejections_advance_only_once():
    for _ in range(8):
        fresh_db()
        f = start_flow()
        tb, tc = f.token_for("준호"), f.token_for("서연")
        results = _race([lambda: approval_flow.decide_with_token(tb, "reject"),
                         lambda: approval_flow.decide_with_token(tc, "reject")])
        assert sorted(r.status for r in results) == ["invalid", "reopened"]
        assert f.version == 2 and len(store.get_proposals(f.sid)) == 2
        assert len(store.get_attempted_slots(f.sid)) == 2


def test_concurrent_final_approvals_trigger_exactly_one_finalize():
    f = start_flow()
    toks = [f.token_for(n) for n in NAMES]
    results = _race([lambda t=t: approval_flow.decide_with_token(t, "approve")
                     for t in toks])
    assert sorted(r.status for r in results) == ["all_approved", "recorded", "recorded"]

    outs = _race([lambda: commit_mod.finalize(f.sid, f.transport),
                  lambda: commit_mod.finalize(f.sid, f.transport)])
    assert all(o.state in ("COMMITTED", "RECHECKING_CALENDARS", "COMMITTING") for o in outs)
    assert f.session["state"] == "COMMITTED"
    assert f.created_events() == 1


# ---- 이벤트 / 양보 잔액은 최종 확정 때만 --------------------------------------------

def test_reject_and_email_failure_leave_events_and_balance_untouched():
    # 이메일 전부 실패
    failing = FakeNotifier()
    failing.fail_all = True
    f = start_flow(notifier=failing)
    assert f.session["state"] == PENDING                           # 성공으로 위장하지 않지만 세션은 재시도 가능
    assert approval_flow.session_view(f.sid)["notification"]["status"] == "all_failed"
    # 거절 + 재제안 뒤에도
    g = start_flow()
    g.reject_and_notify("준호")
    for flow in (f, g):
        assert flow.created_events() == 0
        assert all(abs(v) < 1e-9 for v in flow.balances().values())
        assert store.get_history(GID) == []
        assert flow.session["google_event_id"] is None
        assert flow.session["balance_applied"] == 0


def test_final_unanimous_approval_creates_exactly_one_google_event():
    f = start_flow(agents=_agents_with_different_load())
    f.reject_and_notify("준호")
    second = f.slot_index
    assert f.created_events() == 0

    statuses = [f.approve(n).status for n in NAMES]
    assert statuses == ["recorded", "recorded", "all_approved"]
    assert f.created_events() == 0                                  # 승인만으로는 이벤트가 안 생긴다

    res = commit_mod.finalize(f.sid, f.transport)
    assert res.state == "COMMITTED"
    assert f.created_events() == 1
    assert commit_mod.finalize(f.sid, f.transport).state == "COMMITTED"
    assert f.created_events() == 1                                  # 재호출해도 1개

    ev = list(f.agents[0].backend.created_events.values())[0]
    assert ev["start"][:16] == f.slots[second][0].isoformat()[:16]  # 2순위 슬롯으로 확정

    # 잔액은 확정된 슬롯(2순위)의 regret 으로 정확히 한 번
    hist = store.get_history(GID)
    assert len(hist) == 3
    assert {h["slot_start"] for h in hist} == {f.slots[second][0].isoformat()}
    regrets = json.loads(f.session["proposed_regrets"])
    expected = update_balance({a.agent_id: 0.0 for a in f.agents}, regrets)
    for aid, val in f.balances().items():
        assert abs(val - expected[aid]) < 1e-9
    assert store.apply_balance_once(f.sid) is False
    assert len(store.get_history(GID)) == 3


# ---- 승인 대기 중 캘린더 충돌 (fail-closed 유지) ---------------------------------------

def test_stale_conflict_excludes_slot_and_opens_next_round():
    f = start_flow()
    first = f.slot_index
    old = f.token_for("민지")
    for n in NAMES:
        f.approve(n)
    s, e = f.slots[first]
    f.agents[2].backend.add_busy("primary", s, e)

    res = commit_mod.finalize(f.sid, f.transport)
    assert res.state == "STALE_CONFLICT"
    assert f.created_events() == 0                                  # fail-closed 그대로

    rec = approval_flow.recover_from_stale_conflict(f.sid)
    assert rec.status == "reopened"
    assert f.session["state"] == PENDING
    assert f.slot_index != first and f.version == 2
    assert [p["status"] for p in store.get_proposals(f.sid)] == ["conflict", "open"]
    assert store.get_attempted_slots(f.sid) >= {first, f.slot_index}

    f.dispatch()
    assert len([m for m in f.notifier.outbox if m.proposal_version == 2]) == 3
    assert f.approve("민지", token=old).status == "invalid"          # 이전 링크 무효
    assert store.approval_tally(f.sid) == (0, 0, 3)                  # 전원 재승인 필요
    assert approval_flow.recover_from_stale_conflict(f.sid).status == "invalid"   # 멱등
    assert f.created_events() == 0
    assert all(abs(v) < 1e-9 for v in f.balances().values())


def test_stale_conflict_obeys_the_same_round_limit():
    with MaxRounds(1):
        f = start_flow()
        for n in NAMES:
            f.approve(n)
        s, e = f.slots[f.slot_index]
        f.agents[1].backend.add_busy("primary", s, e)
        assert commit_mod.finalize(f.sid, f.transport).state == "STALE_CONFLICT"
        rec = approval_flow.recover_from_stale_conflict(f.sid)

    assert rec.status == "terminated" and rec.state == "MAX_APPROVAL_ROUNDS_REACHED"
    assert f.session["state"] == "MAX_APPROVAL_ROUNDS_REACHED"
    assert f.created_events() == 0 and f.dispatch().attempted == 0


# ---- 대시보드 뷰 / 종료 후 선택지 ---------------------------------------------------

def test_session_view_hides_links_emails_and_identities():
    f = start_flow()
    old = [f.token_for(n) for n in NAMES]
    f.approve("민지")
    f.reject_and_notify("준호")
    new = [f.token_for(n) for n in NAMES]
    f.approve("서연")
    view = approval_flow.session_view(f.sid)
    blob = json.dumps(view, ensure_ascii=False)

    for t in old + new:
        assert t not in blob
    assert "/approve/" not in blob and "@x.com" not in blob
    assert "regret" not in blob.lower()
    assert view["timelineText"] == "1순위 거절 → 2순위 재제안"
    by_name = {p["name"]: p for p in view["participants"]}
    assert by_name["서연"]["label"] == "응답 완료" and by_name["민지"]["label"] == "발송됨"
    assert all("verdict" not in p for p in view["participants"])    # 동의/거절 구분 없음
    assert view["round"]["current"] == 2 and view["round"]["attempted"] == 2


def test_followup_plan_and_close():
    with MaxRounds(1):
        f = start_flow()
        f.reject("준호")
    assert f.session["state"] == "MAX_APPROVAL_ROUNDS_REACHED"

    wide = approval_flow.followup_plan(f.sid, "widen_range")
    assert wide["days"] == 14 and wide["durationMin"] == 60
    short = approval_flow.followup_plan(f.sid, "shorten_duration")
    assert short["durationMin"] == 30 and short["days"] == 7
    try:
        approval_flow.followup_plan(f.sid, "nonsense")
    except ValueError:
        pass
    else:
        raise AssertionError("알 수 없는 선택이 통과됨")

    assert approval_flow.close_session(f.sid) is True
    assert f.session["state"] == "MAX_APPROVAL_ROUNDS_REACHED"      # 상태는 그대로
    with store.connect() as c:
        row = c.execute("SELECT detail FROM transitions WHERE session_id=? "
                        "ORDER BY id DESC LIMIT 1", (f.sid,)).fetchone()
    assert row["detail"] == "closed by organizer"

    g = start_flow()                                                # 진행 중인 협상에는 적용 불가
    assert approval_flow.close_session(g.sid) is False
    try:
        approval_flow.followup_plan(g.sid, "widen_range")
    except ValueError:
        pass
    else:
        raise AssertionError("진행 중인 협상에 새 협상 선택지가 허용됨")
