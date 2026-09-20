"""스키마 v3 → v4 마이그레이션 (중복 제출 키, 보안 감사 기록, 회의 자료 테이블).

실제 fairmeet.db 는 건드리지 않는다. 현재 스키마로 만든 임시 DB 를 v3 모양으로 되돌린 뒤 검증한다.
"""
import glob
import os
import sqlite3
import tempfile

from common.ids import new_session_id
from common.slots import build_slots
from mediator import store
from tests.flow_helpers import MON

V4_TABLES = ("request_keys", "security_events", "prep_results", "material_shares", "material_tokens")
V5_TABLES = ("change_intakes", "change_events")
V5_TRIGGERS = ("trg_participants_no_late_insert", "trg_participants_no_delete",
               "trg_participants_no_identity_change")
DATA_TABLES = ("sessions", "participants", "proposals", "approvals", "approval_tokens", "notifications",
               "ledger", "history", "transitions", "meeting_drafts", "change_requests")
_paths: list[str] = []


def setup_function(fn=None):
    for p in _paths:
        for f in glob.glob(p.replace(".db", "") + "*"):
            try:
                os.unlink(f)
            except OSError:
                pass
    _paths.clear()


def _rows(path, sql):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def _make_v3_db() -> str:
    """현재 스키마로 만들고 데이터를 넣은 뒤, v4 에서 생긴 것만 지워 v3 모양으로 되돌린다."""
    path = tempfile.mktemp(suffix=".db")
    _paths.append(path)
    store.set_db_path(path)
    store.init_db()
    for i in range(3):
        sid = new_session_id()
        store.create_session(sid, "old-group", "work", 60, build_slots(MON, "work", 60)[:6], [
            {"agent_id": f"grp_a{i}", "display_name": "민지", "email": "a@x.com", "is_organizer": True},
            {"agent_id": f"grp_b{i}", "display_name": "준호", "email": "b@x.com"}])
        with store.connect() as c:
            c.execute("UPDATE sessions SET state = 'COMMITTED', google_event_id = ? WHERE session_id = ?",
                      (f"fmev{i}abc", sid))
    with store.connect() as c:
        c.execute("INSERT INTO ledger VALUES ('old-group', 'grp_a0', 12.5, '2026-09-01T00:00:00+00:00')")
        c.execute("INSERT INTO history (group_id, session_id, agent_id, slot_start, regret, balance_after, "
                  "committed_at) VALUES ('old-group','s','grp_a0','2026-09-21T14:00:00+09:00',0,12.5,'2026-09-01')")
    conn = sqlite3.connect(path)
    for t in V4_TABLES + V5_TABLES:
        conn.execute(f"DROP TABLE {t}")
    for trg in V5_TRIGGERS:
        conn.execute(f"DROP TRIGGER {trg}")
    conn.execute("DROP INDEX IF EXISTS idx_drafts_owner")
    conn.execute("ALTER TABLE meeting_drafts DROP COLUMN owner")
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    return path


def _snapshot(path):
    return {t: [tuple(r) for r in _rows(path, f"SELECT * FROM {t}")] for t in DATA_TABLES}


def test_v3_database_is_backed_up_then_migrated_without_losing_a_row():
    path = _make_v3_db()
    before = _snapshot(path)
    assert _rows(path, "PRAGMA user_version")[0][0] == 3
    store.set_db_path(path)
    report = store.init_db()

    assert (report["from"], report["to"]) == (3, store.SCHEMA_VERSION)
    assert report["backup"] and os.path.exists(report["backup"]) and ".bak-v3-" in report["backup"]
    assert _rows(path, "PRAGMA user_version")[0][0] == store.SCHEMA_VERSION
    names = {r["name"] for r in _rows(path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(V4_TABLES) <= names and set(V5_TABLES) <= names
    assert _snapshot(path) == before                              # 기존 데이터는 하나도 바뀌지 않는다
    assert [tuple(r) for r in _rows(path, "SELECT session_id, state, google_event_id FROM sessions ORDER BY 1")] \
        == [tuple(r) for r in _rows(report["backup"], "SELECT session_id, state, google_event_id FROM sessions ORDER BY 1")]
    assert _rows(report["backup"], "PRAGMA user_version")[0][0] == 3            # 백업은 v3 그대로
    assert not (set(V4_TABLES) & {r["name"] for r in _rows(
        report["backup"], "SELECT name FROM sqlite_master WHERE type='table'")})
    assert _rows(path, "PRAGMA integrity_check")[0][0] == "ok"


def test_v4_migration_is_idempotent_and_makes_no_second_backup():
    path = _make_v3_db()
    store.set_db_path(path)
    first = store.init_db()
    snap = _snapshot(path)
    again = store.init_db()
    assert again["backup"] is None and again["from"] == store.SCHEMA_VERSION
    assert len(glob.glob(path.replace(".db", "") + ".bak-*")) == 1 and os.path.exists(first["backup"])
    assert _snapshot(path) == snap


def test_a_migrated_v3_schema_matches_a_fresh_v4_database():
    old = _make_v3_db()
    store.set_db_path(old)
    store.init_db()
    fresh = tempfile.mktemp(suffix=".db")
    _paths.append(fresh)
    store.set_db_path(fresh)
    store.init_db()

    def shape(p, t):
        return {(r["name"], r["type"], r["notnull"], r["pk"]) for r in _rows(p, f"PRAGMA table_info({t})")}

    for t in DATA_TABLES + V4_TABLES:
        assert shape(old, t) == shape(fresh, t), t
    idx = lambda p: {r["name"] for r in _rows(p, "SELECT name FROM sqlite_master WHERE type='index' "
                                                 "AND name LIKE 'idx_%'")}
    assert idx(old) == idx(fresh)


def test_a_failed_v4_migration_leaves_the_data_untouched_and_can_be_retried():
    path = _make_v3_db()
    before = _snapshot(path)
    store.set_db_path(path)
    good = store.SCHEMA
    broken = tempfile.mktemp(suffix=".sql")
    _paths.append(broken)
    with open(broken, "w", encoding="utf-8") as f:
        f.write(good.read_text(encoding="utf-8") + "\nTHIS IS NOT VALID SQL;\n")
    store.SCHEMA = type(good)(broken)
    try:
        try:
            store.init_db()
        except sqlite3.Error:
            pass
        else:
            raise AssertionError("깨진 마이그레이션이 성공으로 처리됨")
    finally:
        store.SCHEMA = good
    assert _rows(path, "PRAGMA user_version")[0][0] == 3                        # 버전이 올라가지 않았다
    assert _snapshot(path) == before
    assert len(glob.glob(path.replace(".db", "") + ".bak-v3-*")) == 1           # 백업은 남아 있다
    assert store.init_db()["to"] == store.SCHEMA_VERSION                        # 고친 뒤 다시 하면 성공
    assert _snapshot(path) == before


def test_google_event_ids_survive_the_migration():
    path = _make_v3_db()
    store.set_db_path(path)
    store.init_db()
    ids = sorted(r["google_event_id"] for r in _rows(path, "SELECT google_event_id FROM sessions"))
    assert ids == ["fmev0abc", "fmev1abc", "fmev2abc"]
    assert _rows(path, "SELECT concession_balance FROM ledger")[0][0] == 12.5