"""상태 저장소 테스트. 멱등성이 핵심이다."""
import datetime as dt
import tempfile
from pathlib import Path

from common.slots import KST
from mediator import store

SID = "a" * 32
GID = "test-group"
A1 = "grpaaaa_000000000001"
A2 = "grpaaaa_000000000002"

_tmp_db = None


def setup_function(fn=None):
    global _tmp_db
    if _tmp_db and Path(_tmp_db).exists():
        Path(_tmp_db).unlink()
    _tmp_db = tempfile.mktemp(suffix=".db")
    store.set_db_path(_tmp_db)
    store.init_db()
    _make_session()


def _slots(n=5):
    base = dt.datetime(2026, 9, 21, 10, tzinfo=KST)
    return [(base + dt.timedelta(hours=i), base + dt.timedelta(hours=i + 1))
            for i in range(n)]


def _make_session():
    store.create_session(SID, GID, "work", 60, _slots(), [
        {"agent_id": A1, "display_name": "민지", "email": "a@x.com", "is_organizer": 1},
        {"agent_id": A2, "display_name": "준호", "email": "b@x.com"},
    ])


def test_session_created_in_open_state():
    assert store.get_session(SID)["state"] == "OPEN"
    assert len(store.get_participants(SID)) == 2


def test_transition_is_idempotent():
    assert store.transition(SID, "OPEN", "COSTS_COLLECTED") is True
    assert store.transition(SID, "OPEN", "COSTS_COLLECTED") is False
    assert store.get_session(SID)["state"] == "COSTS_COLLECTED"


def test_transition_from_wrong_state_is_noop():
    assert store.transition(SID, "COMMITTING", "COMMITTED") is False
    assert store.get_session(SID)["state"] == "OPEN"


def test_unknown_state_rejected():
    try:
        store.transition(SID, "OPEN", "BANANA")
    except ValueError:
        return
    raise AssertionError("알 수 없는 상태가 통과됨")


def test_regret_matrix_is_persisted_unchanged():
    regret = {A1: [0.0, 10.0, None, 5.0, 2.0], A2: [5.0, 0.0, 1.0, None, 8.0]}
    store.save_regret_matrix(SID, regret, {0, 1, 4})
    loaded, mask, pref = store.load_regret(SID)
    assert loaded == regret
    assert mask == {0, 1, 4}
    assert pref == set()


def test_approval_duplicate_is_ignored():
    store.transition(SID, "OPEN", "COSTS_COLLECTED")
    store.transition(SID, "COSTS_COLLECTED", "AGENT_NEGOTIATING")
    store.open_approval_window(SID, 2, {A1: 0.0, A2: 5.0})
    assert store.record_approval(SID, A1, "approve") == "recorded"
    assert store.record_approval(SID, A1, "approve") == "duplicate"
    assert store.record_approval(SID, A1, "reject") == "duplicate"
    assert store.approval_tally(SID) == (1, 0, 2)


def test_approval_rejected_when_not_pending():
    assert store.record_approval(SID, A1, "approve") == "not_pending"


def test_expired_approval_window():
    store.transition(SID, "OPEN", "COSTS_COLLECTED")
    store.transition(SID, "COSTS_COLLECTED", "AGENT_NEGOTIATING")
    store.open_approval_window(SID, 2, {A1: 0.0, A2: 5.0}, ttl_min=-1)
    assert store.record_approval(SID, A1, "approve") == "not_pending"
    assert SID in store.expire_stale_sessions()
    assert store.get_session(SID)["state"] == "EXPIRED"


def test_balance_only_applies_after_commit():
    store.transition(SID, "OPEN", "COSTS_COLLECTED")
    store.transition(SID, "COSTS_COLLECTED", "AGENT_NEGOTIATING")
    store.open_approval_window(SID, 2, {A1: 0.0, A2: 20.0})
    assert store.apply_balance_once(SID) is False, "COMMITTED 전에 잔액이 적용됨"
    store.transition(SID, "PENDING_HUMAN_APPROVAL", "RECHECKING_CALENDARS")
    store.transition(SID, "RECHECKING_CALENDARS", "COMMITTING")
    store.transition(SID, "COMMITTING", "COMMITTED")
    assert store.apply_balance_once(SID) is True


def test_balance_applied_exactly_once():
    test_balance_only_applies_after_commit()
    first = store.get_balance(GID, [A1, A2])
    assert store.apply_balance_once(SID) is False
    assert store.get_balance(GID, [A1, A2]) == first


def test_balance_sign_positive_means_sacrificed():
    test_balance_only_applies_after_commit()
    bal = store.get_balance(GID, [A1, A2])
    assert bal[A2] > 0 > bal[A1], f"부호가 뒤집힘: {bal}"


def test_history_recorded_on_commit():
    test_balance_only_applies_after_commit()
    rows = store.get_history(GID)
    assert len(rows) == 2


def test_groups_have_separate_ledgers():
    """회사 잔액과 친구 잔액은 별개여야 한다."""
    store.seed_balance("company", {A1: 20.0})
    store.seed_balance("friends", {A1: -5.0})
    assert store.get_balance("company", [A1])[A1] == 20.0
    assert store.get_balance("friends", [A1])[A1] == -5.0


def test_nonce_replay_blocked():
    assert store.check_and_store_nonce("abc123") is True
    assert store.check_and_store_nonce("abc123") is False


def test_participant_lookup_by_token():
    p = store.get_participants(SID)[0]
    found = store.get_participant_by_token(p["approval_token"])
    assert found["agent_id"] == p["agent_id"]
    assert store.get_participant_by_token("nonexistent") is None


def test_clear_approvals_for_renegotiation():
    store.transition(SID, "OPEN", "COSTS_COLLECTED")
    store.transition(SID, "COSTS_COLLECTED", "AGENT_NEGOTIATING")
    store.open_approval_window(SID, 2, {A1: 0.0, A2: 5.0})
    store.record_approval(SID, A1, "approve")
    store.clear_approvals(SID)
    assert store.approval_tally(SID) == (0, 0, 2)
