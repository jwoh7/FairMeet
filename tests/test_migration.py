"""기존 fairmeet.db(재협상 이전 스키마)를 지우지 않고 안전하게 올리는지 검증한다.

LEGACY_SCHEMA 는 재협상 기능 이전 schema.sql 의 사본이다. 실제 fairmeet.db 는
건드리지 않고, 이 스키마로 만든 임시 DB 에서만 검증한다.
"""
import glob
import json
import os
import sqlite3
import tempfile

from mediator import approval_flow, store
from mediator import commit as commit_mod
from tests.flow_helpers import NAMES, start_flow

LEGACY_SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, group_id TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('work', 'social')),
    duration_min INTEGER NOT NULL, state TEXT NOT NULL, slots_json TEXT NOT NULL,
    regret_json TEXT, candidate_mask TEXT, preferred_set TEXT,
    round INTEGER NOT NULL DEFAULT 0, proposed_slot INTEGER, proposed_regrets TEXT,
    google_event_id TEXT, balance_applied INTEGER NOT NULL DEFAULT 0,
    fail_reason TEXT, expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX idx_sessions_state ON sessions(state);
CREATE INDEX idx_sessions_group ON sessions(group_id);
CREATE TABLE participants (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL, agent_url TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL, email TEXT NOT NULL,
    is_organizer INTEGER NOT NULL DEFAULT 0, approval_token TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id));
CREATE UNIQUE INDEX idx_participants_token ON participants(approval_token);
CREATE TABLE approvals (
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('approve', 'reject')),
    decided_at TEXT NOT NULL, PRIMARY KEY (session_id, agent_id));
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
"""

SLOTS = json.dumps([[f"2026-09-21T{9 + i:02d}:00:00+09:00",
                     f"2026-09-21T{10 + i:02d}:00:00+09:00"] for i in range(8)])
A1, A2 = "grpaaaa_000000000001", "grpaaaa_000000000002"
FUTURE = "2999-01-01T00:00:00+00:00"
TS = "2026-09-01T00:00:00+00:00"

# (session_id, state, proposed_slot, google_event_id, balance_applied)
SESSIONS = [
    ("s_pending", "PENDING_HUMAN_APPROVAL", 3, None, 0),
    ("s_commit", "COMMITTED", 5, "fmabc123", 1),
    ("s_reject", "REJECTED", 2, None, 0),
    ("s_nofeas", "NO_FEASIBLE_SLOT", None, None, 0),
]
APPROVALS = [("s_pending", A1, "approve"), ("s_commit", A1, "approve"),
             ("s_commit", A2, "approve"), ("s_reject", A2, "reject")]

_paths: list[str] = []


def _make_legacy_db() -> str:
    path = tempfile.mktemp(suffix=".db")
    _paths.append(path)
    c = sqlite3.connect(path)
    c.executescript(LEGACY_SCHEMA)
    for sid, state, slot, ev, applied in SESSIONS:
        c.execute(
            "INSERT INTO sessions (session_id, group_id, mode, duration_min, state, "
            "slots_json, proposed_slot, proposed_regrets, google_event_id, balance_applied, "
            "expires_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "old-group", "work", 60, state, SLOTS, slot,
             json.dumps({A1: 0.0, A2: 10.0}) if slot is not None else None,
             ev, applied, FUTURE, TS, TS))
        for i, (aid, nm) in enumerate(((A1, "민지"), (A2, "준호"))):
            c.execute("INSERT INTO participants (session_id, agent_id, display_name, "
                      "email, is_organizer, approval_token) VALUES (?,?,?,?,?,?)",
                      (sid, aid, nm, f"u{i}@x.com", int(i == 0), f"legacy-{sid}-{i}"))
        c.execute("INSERT INTO transitions (session_id, from_state, to_state, at) "
                  "VALUES (?, NULL, 'OPEN', ?)", (sid, TS))
    for sid, aid, verdict in APPROVALS:
        c.execute("INSERT INTO approvals VALUES (?,?,?,?)", (sid, aid, verdict, TS))
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


def _table_shape(path, table):
    return {(r["name"], r["type"], r["notnull"], r["pk"])
            for r in _rows(path, f"PRAGMA table_info({table})")}


def test_migration_preserves_every_row_and_backs_up_first():
    path = _make_legacy_db()
    before = {t: [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")]
              for t in ("sessions", "participants", "ledger", "history", "transitions")}
    store.set_db_path(path)
    report = store.init_db()

    assert (report["from"], report["to"]) == (0, store.SCHEMA_VERSION)
    assert report["backup"] and os.path.exists(report["backup"])
    assert report["backup"].endswith(".db")                       # .gitignore 의 *.db 에 걸린다
    assert _rows(path, "PRAGMA user_version")[0][0] == store.SCHEMA_VERSION

    # 백업은 이전 스키마 그대로다 (되돌릴 수 있다)
    assert "proposal_version" not in {r["name"] for r in
                                      _rows(report["backup"], "PRAGMA table_info(approvals)")}
    assert len(_rows(report["backup"], "SELECT * FROM approvals")) == len(APPROVALS)

    # 기존 데이터는 하나도 잃지 않았다 (sessions 는 새 컬럼이 뒤에 붙을 뿐)
    for t in ("participants", "ledger", "history", "transitions"):
        assert [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")] == before[t], t
    old_cols = len(before["sessions"][0])
    assert [tuple(r)[:old_cols] for r in _rows(path, "SELECT * FROM sessions")] == before["sessions"]

    ap = _rows(path, "SELECT session_id, agent_id, proposal_version, verdict FROM approvals "
                     "ORDER BY session_id, agent_id")
    assert [(r["session_id"], r["agent_id"], r["verdict"]) for r in ap] == \
        sorted(APPROVALS, key=lambda x: (x[0], x[1]))
    assert {r["proposal_version"] for r in ap} == {1}             # 이전 응답은 모두 1번 제안의 것


def test_migration_backfills_proposals_from_existing_sessions():
    path = _make_legacy_db()
    store.set_db_path(path)
    store.init_db()

    ver = {r["session_id"]: r["proposal_version"]
           for r in _rows(path, "SELECT session_id, proposal_version FROM sessions")}
    assert ver == {"s_pending": 1, "s_commit": 1, "s_reject": 1, "s_nofeas": 0}
    props = {r["session_id"]: (r["version"], r["slot_index"], r["status"])
             for r in _rows(path, "SELECT * FROM proposals")}
    assert props == {"s_pending": (1, 3, "open"), "s_commit": (1, 5, "approved"),
                     "s_reject": (1, 2, "rejected")}
    # 이미 시도한 슬롯이라는 사실이 그대로 기록된다
    assert _rows(path, "SELECT COUNT(*) n FROM notifications")[0]["n"] == 0


def test_migration_is_idempotent_and_takes_no_second_backup():
    path = _make_legacy_db()
    store.set_db_path(path)
    first = store.init_db()
    snapshot = {t: [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")]
                for t in ("sessions", "approvals", "proposals", "ledger")}

    again = store.init_db()
    assert again["backup"] is None and again["from"] == store.SCHEMA_VERSION
    assert again["sessions_backfilled"] == 0 and again["approvals_rebuilt"] is False
    assert len(glob.glob(path.replace(".db", "") + ".bak-*")) == 1
    assert os.path.exists(first["backup"])
    for t, rows in snapshot.items():
        assert [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")] == rows, t


def test_migrated_schema_matches_a_fresh_database():
    legacy = _make_legacy_db()
    store.set_db_path(legacy)
    store.init_db()
    fresh = tempfile.mktemp(suffix=".db")
    _paths.append(fresh)
    store.set_db_path(fresh)
    store.init_db()
    for table in ("sessions", "approvals", "proposals", "approval_tokens",
                  "notifications", "participants", "ledger", "history"):
        assert _table_shape(legacy, table) == _table_shape(fresh, table), table


def test_fresh_database_needs_no_backup():
    path = tempfile.mktemp(suffix=".db")
    _paths.append(path)
    store.set_db_path(path)
    report = store.init_db()
    assert report["backup"] is None and report["from"] == 0
    assert glob.glob(path.replace(".db", "") + ".bak-*") == []


def test_failed_migration_rolls_back_completely_and_can_be_retried():
    path = _make_legacy_db()
    store.set_db_path(path)
    good = store._APPROVALS_V2_DDL
    store._APPROVALS_V2_DDL = "CREATE TABLE approvals_v2 (THIS IS NOT VALID SQL"
    try:
        try:
            store.init_db()
        except sqlite3.Error:
            pass
        else:
            raise AssertionError("깨진 마이그레이션이 성공으로 처리됨")
    finally:
        store._APPROVALS_V2_DDL = good

    # 절반만 적용된 상태가 남지 않는다: 새 컬럼도, 새 버전도 없다
    assert "proposal_version" not in {r["name"] for r in _rows(path, "PRAGMA table_info(sessions)")}
    assert "proposal_version" not in {r["name"] for r in _rows(path, "PRAGMA table_info(approvals)")}
    assert _rows(path, "PRAGMA user_version")[0][0] == 0
    assert len(_rows(path, "SELECT * FROM approvals")) == len(APPROVALS)

    assert store.init_db()["to"] == store.SCHEMA_VERSION           # 고친 뒤 다시 하면 성공
    assert len(_rows(path, "SELECT * FROM approvals")) == len(APPROVALS)


def test_legacy_pending_session_is_safe_and_new_flows_work_on_migrated_db():
    path = _make_legacy_db()
    store.set_db_path(path)
    store.init_db()

    # 예전 승인 대기 세션: 화면은 그려지지만 옛 토큰으로는 아무도 응답할 수 없다
    view = approval_flow.session_view("s_pending")
    assert view["state"] == "PENDING_HUMAN_APPROVAL" and view["timelineText"] == "1순위 제안"
    assert approval_flow.inspect_token("legacy-s_pending-0") is None
    assert approval_flow.decide_with_token("legacy-s_pending-0", "approve").status == "invalid"
    assert store.record_approval("s_pending", A1, "approve", proposal_version=1) == "duplicate"

    # 마이그레이션된 DB 위에서 새 흐름(거절 → 재제안 → 전원 승인 → 커밋)이 그대로 동작한다
    f = start_flow()                                                # 방금 마이그레이션한 DB 를 쓴다
    assert f.reject_and_notify("준호").status == "reopened"
    for n in NAMES:
        f.approve(n)
    assert commit_mod.finalize(f.sid, f.transport).state == "COMMITTED"
    assert f.created_events() == 1
    # 예전 데이터의 잔액은 그대로다
    assert store.get_balance("old-group", [A1, A2]) == {A1: 12.5, A2: -12.5}
    assert len(store.get_history("old-group")) == 1
