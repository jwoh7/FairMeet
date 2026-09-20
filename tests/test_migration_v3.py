"""스키마 v2(재협상 도입 뒤) → v3(정족수·회의 조건·자연어 초안·일정 변경) 마이그레이션.

V2_SCHEMA 는 v3 이전 schema.sql 의 사본이다. 실제 fairmeet.db 는 건드리지 않고,
이 스키마로 만든 임시 DB 에서만 검증한다.
"""
import glob
import json
import os
import sqlite3
import tempfile

from mediator import approval_flow, store

V2_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('work', 'social')),
    duration_min INTEGER NOT NULL, state TEXT NOT NULL, slots_json TEXT NOT NULL,
    regret_json TEXT, candidate_mask TEXT, preferred_set TEXT,
    round INTEGER NOT NULL DEFAULT 0, proposed_slot INTEGER, proposed_regrets TEXT,
    google_event_id TEXT, balance_applied INTEGER NOT NULL DEFAULT 0,
    fail_reason TEXT, expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    proposal_version INTEGER NOT NULL DEFAULT 0, lam REAL);
CREATE INDEX idx_sessions_state ON sessions(state);
CREATE INDEX idx_sessions_group ON sessions(group_id);
CREATE TABLE participants (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL, agent_url TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL, email TEXT NOT NULL,
    is_organizer INTEGER NOT NULL DEFAULT 0, approval_token TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id));
CREATE UNIQUE INDEX idx_participants_token ON participants(approval_token);
CREATE TABLE proposals (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    version INTEGER NOT NULL, slot_index INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'approved', 'rejected', 'conflict', 'expired')),
    opened_at TEXT NOT NULL, closed_at TEXT,
    PRIMARY KEY (session_id, version), UNIQUE (session_id, slot_index));
CREATE TABLE approvals (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL, proposal_version INTEGER NOT NULL DEFAULT 1,
    verdict TEXT NOT NULL CHECK (verdict IN ('approve', 'reject')),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (session_id, proposal_version, agent_id));
CREATE TABLE approval_tokens (
    jti TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    proposal_version INTEGER NOT NULL, agent_id TEXT NOT NULL, expires_at TEXT NOT NULL,
    used_at TEXT, revoked_at TEXT, created_at TEXT NOT NULL);
CREATE INDEX idx_tokens_target ON approval_tokens(session_id, proposal_version, agent_id);
CREATE TABLE notifications (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    proposal_version INTEGER NOT NULL, agent_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, backend TEXT, sent_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, proposal_version, agent_id));
CREATE TABLE ledger (
    group_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    concession_balance REAL NOT NULL DEFAULT 0.0, updated_at TEXT NOT NULL,
    PRIMARY KEY (group_id, agent_id));
CREATE TABLE history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
    session_id TEXT NOT NULL, agent_id TEXT NOT NULL, slot_start TEXT NOT NULL,
    regret REAL NOT NULL, balance_after REAL NOT NULL, committed_at TEXT NOT NULL);
CREATE TABLE transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    from_state TEXT, to_state TEXT NOT NULL, detail TEXT, at TEXT NOT NULL);
CREATE TABLE seen_nonces (nonce TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
PRAGMA user_version = 2;
"""

SLOTS = json.dumps([[f"2026-09-21T{9 + i:02d}:00:00+09:00",
                     f"2026-09-21T{10 + i:02d}:00:00+09:00"] for i in range(8)])
A1, A2 = "grpaaaa_000000000001", "grpaaaa_000000000002"
FUTURE = "2999-01-01T00:00:00+00:00"
PAST = "2000-01-01T00:00:00+00:00"
TS = "2026-09-01T00:00:00+00:00"

# (session_id, state, proposed_slot, proposal_version, event_id, expires_at)
SESSIONS = [
    ("s_pending", "PENDING_HUMAN_APPROVAL", 3, 1, None, FUTURE),
    ("s_expired", "PENDING_HUMAN_APPROVAL", 4, 1, None, PAST),     # 마이그레이션이 건드리면 안 된다
    ("s_commit", "COMMITTED", 5, 1, "fmabc123", FUTURE),
    ("s_exhaust", "CANDIDATES_EXHAUSTED", 2, 2, None, FUTURE),
]

_paths: list[str] = []


def _make_v2_db() -> str:
    path = tempfile.mktemp(suffix=".db")
    _paths.append(path)
    c = sqlite3.connect(path)
    c.executescript(V2_SCHEMA)
    for sid, state, slot, ver, ev, exp in SESSIONS:
        c.execute(
            "INSERT INTO sessions (session_id, group_id, mode, duration_min, state, slots_json, "
            "proposed_slot, proposed_regrets, google_event_id, balance_applied, expires_at, "
            "created_at, updated_at, proposal_version, lam) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "old-group", "work", 60, state, SLOTS, slot,
             json.dumps({A1: 0.0, A2: 10.0}), ev, int(ev is not None), exp, TS, TS, ver, 0.6))
        for i, (aid, nm) in enumerate(((A1, "민지"), (A2, "준호"))):
            c.execute("INSERT INTO participants (session_id, agent_id, display_name, email, "
                      "is_organizer, approval_token) VALUES (?,?,?,?,?,?)",
                      (sid, aid, nm, f"u{i}@x.com", int(i == 0), f"legacy-{sid}-{i}"))
        c.execute("INSERT INTO proposals VALUES (?,?,?,?,?,?)",
                  (sid, 1, slot, "open" if state == "PENDING_HUMAN_APPROVAL" else "approved",
                   TS, None))
        c.execute("INSERT INTO transitions (session_id, from_state, to_state, at) "
                  "VALUES (?, NULL, 'OPEN', ?)", (sid, TS))
    c.execute("INSERT INTO approvals VALUES ('s_commit', ?, 1, 'approve', ?)", (A1, TS))
    c.execute("INSERT INTO approvals VALUES ('s_commit', ?, 1, 'approve', ?)", (A2, TS))
    c.execute("INSERT INTO ledger VALUES ('old-group', ?, 12.5, ?)", (A1, TS))
    c.execute("INSERT INTO ledger VALUES ('old-group', ?, -12.5, ?)", (A2, TS))
    c.execute("INSERT INTO history (group_id, session_id, agent_id, slot_start, regret, "
              "balance_after, committed_at) VALUES ('old-group','s_commit',?,?,0,12.5,?)",
              (A1, "2026-09-21T14:00:00+09:00", TS))
    c.commit()
    c.close()
    return path


def setup_function(fn=None):
    for p in _paths:
        for f in glob.glob(p.replace(".db", "") + "*"):
            try:
                os.unlink(f)
            except OSError:
                pass
    _paths.clear()


def _rows(path, sql, args=()):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _cols(path, table):
    return [r["name"] for r in _rows(path, f"PRAGMA table_info({table})")]


V3_NEW_SESSION_COLS = {"approval_policy_json", "approvals_needed", "required_agents_json",
                       "constraints_json", "slot_effects_json", "parent_session_id",
                       "change_request_id"}
V3_NEW_TABLES = ("meeting_drafts", "change_requests", "change_votes", "change_tokens",
                 "change_mail")
DATA_TABLES = ("sessions", "participants", "proposals", "approvals", "approval_tokens",
               "notifications", "ledger", "history", "transitions")


def test_v2_database_is_backed_up_then_migrated_without_losing_a_row():
    path = _make_v2_db()
    before = {t: [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")] for t in DATA_TABLES}
    store.set_db_path(path)
    report = store.init_db()

    assert (report["from"], report["to"]) == (2, store.SCHEMA_VERSION)
    assert report["backup"] and os.path.exists(report["backup"])
    assert report["v3_columns_added"] == len(V3_NEW_SESSION_COLS)
    assert _rows(path, "PRAGMA user_version")[0][0] == store.SCHEMA_VERSION

    # 백업은 v2 그대로다 (되돌릴 수 있다)
    assert _rows(report["backup"], "PRAGMA user_version")[0][0] == 2
    assert not (V3_NEW_SESSION_COLS & set(_cols(report["backup"], "sessions")))
    assert [tuple(r) for r in _rows(report["backup"], "SELECT * FROM sessions")] == before["sessions"]

    # 원본 데이터는 하나도 잃지 않았다. sessions 는 새 컬럼이 뒤에 붙을 뿐이다.
    for t in DATA_TABLES:
        old_cols = len(_cols(report["backup"], t))
        got = [tuple(r)[:old_cols] for r in _rows(path, f"SELECT * FROM {t}")]
        assert got == before[t], t
    assert V3_NEW_SESSION_COLS <= set(_cols(path, "sessions"))
    assert set(V3_NEW_TABLES) <= {r["name"] for r in _rows(
        path, "SELECT name FROM sqlite_master WHERE type='table'")}

    # 기존 세션은 전원 승인·조건 없음으로 남는다
    for r in _rows(path, "SELECT * FROM sessions"):
        assert r["approval_policy_json"] == '{"mode":"all"}' and r["approvals_needed"] is None
        assert r["required_agents_json"] == "[]" and r["constraints_json"] is None
        assert r["slot_effects_json"] is None and r["parent_session_id"] is None


def test_migration_does_not_expire_or_otherwise_touch_session_states():
    """만료 처리는 서버가 기동 때 따로 한다. 마이그레이션은 상태를 바꾸지 않는다."""
    path = _make_v2_db()
    store.set_db_path(path)
    store.init_db()
    states = {r["session_id"]: r["state"] for r in _rows(path, "SELECT * FROM sessions")}
    assert states["s_expired"] == "PENDING_HUMAN_APPROVAL"
    assert states["s_commit"] == "COMMITTED" and states["s_exhaust"] == "CANDIDATES_EXHAUSTED"

    # 이후 서버가 하는 정리는 만료된 승인 대기 세션만 EXPIRED 로 바꾼다
    assert store.expire_stale_sessions() == ["s_expired"]
    states = {r["session_id"]: r["state"] for r in _rows(path, "SELECT * FROM sessions")}
    assert states == {"s_pending": "PENDING_HUMAN_APPROVAL", "s_expired": "EXPIRED",
                      "s_commit": "COMMITTED", "s_exhaust": "CANDIDATES_EXHAUSTED"}


def test_v3_migration_is_idempotent_and_takes_no_second_backup():
    path = _make_v2_db()
    store.set_db_path(path)
    first = store.init_db()
    snapshot = {t: [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")] for t in DATA_TABLES}
    again = store.init_db()
    assert again["backup"] is None and again["from"] == store.SCHEMA_VERSION and again["v3_columns_added"] == 0
    assert len(glob.glob(path.replace(".db", "") + ".bak-*")) == 1
    assert os.path.exists(first["backup"])
    for t, rows in snapshot.items():
        assert [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")] == rows, t


def test_migrated_v2_schema_matches_a_fresh_v3_database():
    old = _make_v2_db()
    store.set_db_path(old)
    store.init_db()
    fresh = tempfile.mktemp(suffix=".db")
    _paths.append(fresh)
    store.set_db_path(fresh)
    store.init_db()

    def shape(p, t):
        return {(r["name"], r["type"], r["notnull"], r["pk"])
                for r in _rows(p, f"PRAGMA table_info({t})")}

    for t in DATA_TABLES + V3_NEW_TABLES:
        assert shape(old, t) == shape(fresh, t), t
    trig = lambda p: {r["name"] for r in _rows(
        p, "SELECT name FROM sqlite_master WHERE type='trigger'")}
    v5 = {"trg_participants_no_late_insert", "trg_participants_no_delete",
          "trg_participants_no_identity_change"}                      # v5 가 추가한 참가자 보호 트리거
    assert trig(old) == trig(fresh) == {"trg_sessions_policy_immutable"} | v5


def test_failed_v3_migration_leaves_the_original_untouched_and_can_be_retried():
    path = _make_v2_db()
    before = {t: [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")] for t in DATA_TABLES}
    store.set_db_path(path)
    good = store._V3_SESSION_COLUMNS
    store._V3_SESSION_COLUMNS = good + (("broken_column", "THIS IS NOT VALID SQL"),)
    try:
        try:
            store.init_db()
        except sqlite3.Error:
            pass
        else:
            raise AssertionError("깨진 마이그레이션이 성공으로 처리됨")
    finally:
        store._V3_SESSION_COLUMNS = good

    # 절반만 적용된 상태가 없다: 새 컬럼도, 새 버전도, 트리거도 없다
    assert not (V3_NEW_SESSION_COLS & set(_cols(path, "sessions")))
    assert _rows(path, "PRAGMA user_version")[0][0] == 2
    assert _rows(path, "SELECT COUNT(*) n FROM sqlite_master WHERE type='trigger' "
                       "AND name = 'trg_sessions_policy_immutable'")[0]["n"] == 0
    for t in DATA_TABLES:
        assert [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")] == before[t], t
    backups = glob.glob(path.replace(".db", "") + ".bak-v2-*")
    assert len(backups) == 1                                  # 실패해도 백업은 남아 있다

    assert store.init_db()["to"] == store.SCHEMA_VERSION                          # 고친 뒤 다시 하면 성공
    for t in DATA_TABLES:
        old_cols = len(_cols(backups[0], t))
        assert [tuple(r)[:old_cols] for r in _rows(path, f"SELECT * FROM {t}")] == before[t], t


def test_migrated_pending_session_keeps_all_approve_semantics():
    path = _make_v2_db()
    store.set_db_path(path)
    store.init_db()

    view = approval_flow.session_view("s_pending")
    assert view["policy"]["mode"] == "all" and view["policy"]["required"] == []
    # 전원 승인: 1명의 동의로는 확정되지 않고, 두 번째 동의에서 확정된다
    assert approval_flow.decide_for_agent("s_pending", A1, "approve", 1).status == "recorded"
    assert approval_flow.decide_for_agent("s_pending", A2, "approve", 1).status == "all_approved"
    # 이전 세션의 원장은 그대로다
    assert store.get_balance("old-group", [A1, A2]) == {A1: 12.5, A2: -12.5}
    assert len(store.get_history("old-group")) == 1
