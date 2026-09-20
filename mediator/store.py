"""협상 세션의 영속화와 상태 전이.

모든 전이는 `WHERE state = <예상 이전 상태>` 조건부 UPDATE 로 수행하고,
갱신 행 수가 0이면 다른 요청이 이미 전이시킨 것으로 보고 조용히 False 를
반환한다. 이것이 중복 클릭·재전송·동시 요청에 대한 멱등성의 근거다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from common import config

log = logging.getLogger("fairmeet.store")

SCHEMA = Path(__file__).with_name("schema.sql")

# 스키마 버전 (PRAGMA user_version).
#   0  승인 재협상 도입 이전 (approvals 의 PK 가 (session_id, agent_id))
#   2  proposals / approval_tokens / notifications 추가, approvals 에 proposal_version
#   3  승인 정책(정족수·필수 참석자), 회의 조건, 자연어 초안, 일정 변경 요청 테이블
#   4  중복 제출 방지 키, 보안 감사 기록, 회의 자료 분석 결과·전달·열람 토큰 (새 테이블만 추가)
#   5  초안 소유자(owner), 변경 요청 접수(change_intakes)와 처리 이력(change_events), 참가자 스냅샷 보호 트리거
SCHEMA_VERSION = 5

# 종료 상태. 여기 들어가면 이메일 발송, 캘린더 이벤트 생성, 잔액 변경이 더는 없다.
#   REJECTED          — 재협상 도입 이전 세션에만 남아 있는 상태 (새 흐름은 만들지 않음)
#   NO_FEASIBLE_SLOT  — 에이전트 협상 단계에서 합의 실패
#   NO_COMMON_SLOT / CANDIDATES_EXHAUSTED / MAX_APPROVAL_ROUNDS_REACHED
#                     — 사람 승인 흐름의 종료 (approval_flow)
#   CANCELLED / RESCHEDULED
#                     — 확정된 회의가 변경 요청으로 취소/이동된 뒤의 상태 (change_flow)
TERMINAL_STATES = frozenset({
    "COMMITTED", "REJECTED", "EXPIRED", "NO_FEASIBLE_SLOT",
    "AGENT_UNAVAILABLE", "COMMIT_FAILED",
    "NO_COMMON_SLOT", "CANDIDATES_EXHAUSTED", "MAX_APPROVAL_ROUNDS_REACHED",
    "CANCELLED", "RESCHEDULED",
})

ALL_STATES = frozenset({
    "OPEN", "COSTS_COLLECTED", "AGENT_NEGOTIATING", "PENDING_HUMAN_APPROVAL",
    "RECHECKING_CALENDARS", "COMMITTING", "STALE_CONFLICT",
}) | TERMINAL_STATES

_db_path = config.DB_PATH


def set_db_path(path: str) -> None:
    """테스트에서 임시 DB 를 쓰기 위한 훅."""
    global _db_path
    _db_path = path


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


@contextmanager
def connect():
    conn = sqlite3.connect(_db_path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _has_table(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


# schema.sql 의 approvals 정의와 같아야 한다 (tests/test_migration.py 가 대조한다).
_APPROVALS_V2_DDL = """
CREATE TABLE approvals_v2 (
    session_id       TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id         TEXT NOT NULL,
    proposal_version INTEGER NOT NULL DEFAULT 1,
    verdict          TEXT NOT NULL CHECK (verdict IN ('approve', 'reject')),
    decided_at       TEXT NOT NULL,
    PRIMARY KEY (session_id, proposal_version, agent_id)
)"""


# v3 에서 sessions 에 붙는 컬럼. schema.sql 의 sessions 정의와 같아야 한다.
# 기존 행은 DEFAULT 로 채워진다: 정책 = 전원 승인, 필수 참석자 없음, 조건 없음.
_V3_SESSION_COLUMNS = (
    ("approval_policy_json", "TEXT NOT NULL DEFAULT '{\"mode\":\"all\"}'"),
    ("approvals_needed", "INTEGER"),
    ("required_agents_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("constraints_json", "TEXT"),
    ("slot_effects_json", "TEXT"),
    ("parent_session_id", "TEXT"),
    ("change_request_id", "TEXT"),
)

# 승인 정책과 회의 조건은 세션 생성 때 정해지고 이후 아무도 바꿀 수 없다.
_POLICY_IMMUTABLE_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS trg_sessions_policy_immutable
BEFORE UPDATE OF approval_policy_json, approvals_needed, required_agents_json,
                 constraints_json, slot_effects_json ON sessions
WHEN NEW.approval_policy_json IS NOT OLD.approval_policy_json
  OR NEW.approvals_needed IS NOT OLD.approvals_needed
  OR NEW.required_agents_json IS NOT OLD.required_agents_json
  OR NEW.constraints_json IS NOT OLD.constraints_json
  OR NEW.slot_effects_json IS NOT OLD.slot_effects_json
BEGIN
  SELECT RAISE(ABORT, 'session policy and constraints are immutable');
END"""


def _backup_before_migration(conn, from_version: int) -> str | None:
    """기존 데이터가 있는 DB 를 마이그레이션하기 전에 통째로 복사해 둔다.

    SQLite 백업 API 를 쓰므로 WAL 모드에서 다른 프로세스가 열어 두고 있어도
    일관된 스냅샷이 만들어진다. 파일명은 *.db 로 끝나 .gitignore 에 걸린다.
    """
    if _db_path in (":memory:", ""):
        return None
    src = Path(_db_path)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = src.with_name(f"{src.stem}.bak-v{from_version}-{stamp}.db")
    out = sqlite3.connect(str(dest))
    try:
        conn.backup(out)
    finally:
        out.close()
    return str(dest)


def _migrate(conn) -> dict:
    """구 스키마를 현재 스키마로 올린다. 여러 번 실행해도 안전하다(멱등).

    데이터는 지우지 않는다. 한 트랜잭션에서 수행하므로 중간에 실패하면
    아무것도 바뀌지 않는다.
    """
    report = {"sessions_backfilled": 0, "approvals_rebuilt": False,
              "v3_columns_added": 0, "v5_columns_added": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = _columns(conn, "sessions")
        if "proposal_version" not in cols:
            conn.execute("ALTER TABLE sessions "
                         "ADD COLUMN proposal_version INTEGER NOT NULL DEFAULT 0")
        if "lam" not in cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN lam REAL")

        # approvals: PK 에 proposal_version 을 넣으려면 테이블을 다시 만들어야 한다.
        # 기존 승인 행은 모두 1번 제안의 것으로 옮긴다 (재협상 도입 전에는 제안이 1개뿐).
        if "proposal_version" not in _columns(conn, "approvals"):
            conn.execute(_APPROVALS_V2_DDL)
            conn.execute(
                "INSERT INTO approvals_v2 "
                "(session_id, agent_id, proposal_version, verdict, decided_at) "
                "SELECT session_id, agent_id, 1, verdict, decided_at FROM approvals")
            conn.execute("DROP TABLE approvals")
            conn.execute("ALTER TABLE approvals_v2 RENAME TO approvals")
            report["approvals_rebuilt"] = True

        # 이미 제안이 있었던 세션은 1번 제안으로 소급 기록한다.
        cur = conn.execute(
            "UPDATE sessions SET proposal_version = 1 "
            "WHERE proposed_slot IS NOT NULL AND proposal_version = 0")
        report["sessions_backfilled"] = cur.rowcount
        conn.execute(
            "INSERT OR IGNORE INTO proposals "
            "(session_id, version, slot_index, status, opened_at, closed_at) "
            "SELECT session_id, 1, proposed_slot, "
            "  CASE state WHEN 'PENDING_HUMAN_APPROVAL' THEN 'open' "
            "             WHEN 'REJECTED' THEN 'rejected' "
            "             WHEN 'STALE_CONFLICT' THEN 'conflict' "
            "             WHEN 'EXPIRED' THEN 'expired' "
            "             ELSE 'approved' END, "
            "  created_at, "
            "  CASE state WHEN 'PENDING_HUMAN_APPROVAL' THEN NULL ELSE updated_at END "
            "FROM sessions WHERE proposed_slot IS NOT NULL")

        # v3: 승인 정책·회의 조건 컬럼. 기존 세션은 DEFAULT 로 '전원 승인'이 된다.
        have = _columns(conn, "sessions")
        for name, ddl in _V3_SESSION_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")
                report["v3_columns_added"] += 1
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_parent "
                     "ON sessions(parent_session_id)")
        conn.execute(_POLICY_IMMUTABLE_TRIGGER)

        # v5: 초안 소유자. 기존 초안은 소유자 없음('') 이라 제품 화면에서는 열리지 않는다.
        if "owner" not in _columns(conn, "meeting_drafts"):
            conn.execute("ALTER TABLE meeting_drafts ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            report["v5_columns_added"] = report.get("v5_columns_added", 0) + 1
        conn.execute("CREATE INDEX IF NOT EXISTS idx_drafts_owner ON meeting_drafts(owner, status)")

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return report


def init_db() -> dict:
    """스키마를 만들고, 구 DB 라면 안전하게 마이그레이션한다.

    Returns: {"from": 이전 버전, "to": 현재 버전, "backup": 백업 경로|None, ...}
    """
    conn = sqlite3.connect(_db_path, isolation_level=None, timeout=10.0)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        legacy = _has_table(conn, "sessions") and version < SCHEMA_VERSION
        backup = _backup_before_migration(conn, version) if legacy else None

        # 새 테이블은 IF NOT EXISTS 로 추가되고, 기존 테이블은 건드리지 않는다.
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        report = _migrate(conn)
    finally:
        conn.close()

    report.update({"from": version, "to": SCHEMA_VERSION, "backup": backup})
    if legacy:
        log.info("DB 마이그레이션 v%s → v%s 완료 (백업: %s, 소급 기록 %s건)",
                 version, SCHEMA_VERSION, backup, report["sessions_backfilled"])
    return report


def reset_db() -> None:
    """테스트 전용. 모든 테이블을 비운다."""
    with connect() as c:
        c.execute("DELETE FROM sessions")          # 참가자 등 딸린 행은 CASCADE 로 지워진다 (참가자 보호 트리거 때문)
        for t in ("approval_tokens", "notifications", "proposals", "approvals",
                  "participants", "history", "transitions",
                  "seen_nonces", "ledger", "request_keys", "security_events",
                  "change_events", "meeting_drafts"):
            c.execute(f"DELETE FROM {t}")


# ---- 생성 -----------------------------------------------------------------

def create_session(session_id, group_id, mode, duration_min, slots,
                   participants, *, policy=None, constraints=None,
                   slot_effects=None, parent_session_id=None,
                   change_request_id=None) -> None:
    """participants: [{agent_id, display_name, email, is_organizer, agent_url?}]

    policy (mediator.policy.ApprovalPolicy), constraints(dict), slot_effects(dict) 는
    세션 생성 때 한 번만 저장되고 이후 바뀌지 않는다. 주지 않으면 '전원 승인, 조건 없음'.
    """
    from mediator import policy as policy_mod

    pol = policy or policy_mod.ALL_APPROVE
    ts = now()
    slots_json = json.dumps([[s.isoformat(), e.isoformat()] for s, e in slots])
    with connect() as c:
        c.execute(
            """INSERT INTO sessions
               (session_id, group_id, mode, duration_min, state, slots_json,
                round, balance_applied, created_at, updated_at,
                approval_policy_json, approvals_needed, required_agents_json,
                constraints_json, slot_effects_json, parent_session_id,
                change_request_id)
               VALUES (?,?,?,?,'OPEN',?,0,0,?,?,?,?,?,?,?,?,?)""",
            (session_id, group_id, mode, duration_min, slots_json, ts, ts,
             pol.to_json(), pol.needed(len(participants)) if pol.mode != "all" else None,
             json.dumps(list(pol.required)),
             json.dumps(constraints, ensure_ascii=False) if constraints is not None else None,
             json.dumps(slot_effects) if slot_effects is not None else None,
             parent_session_id, change_request_id))
        for p in participants:
            c.execute(
                """INSERT INTO participants
                   (session_id, agent_id, agent_url, display_name, email,
                    is_organizer, approval_token)
                   VALUES (?,?,?,?,?,?,?)""",
                (session_id, p["agent_id"], p.get("agent_url", ""),
                 p["display_name"], p["email"], int(p.get("is_organizer", 0)),
                 # 레거시 NOT NULL/UNIQUE 컬럼을 채우는 값일 뿐, 승인에는 쓰이지 않는다.
                 # 실제 승인 링크는 tokens.mint() 가 발송 시점에 만든다.
                 "legacy-unused-" + secrets.token_hex(8)))
        c.execute("INSERT INTO transitions (session_id, from_state, to_state, at) "
                  "VALUES (?, NULL, 'OPEN', ?)", (session_id, ts))


# ---- 조회 -----------------------------------------------------------------

def get_session(session_id) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM sessions WHERE session_id = ?",
                         (session_id,)).fetchone()


def get_participants(session_id) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM participants WHERE session_id = ? ORDER BY agent_id",
            (session_id,)).fetchall()


def get_participant_by_token(token: str) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM participants WHERE approval_token = ?",
                         (token,)).fetchone()


def _balance_tx(c, group_id, agent_ids) -> dict[str, float]:
    """호출자의 트랜잭션 안에서 잔액을 읽는다 (새 연결을 열면 락에 걸린다)."""
    rows = c.execute(
        "SELECT agent_id, concession_balance FROM ledger WHERE group_id = ?",
        (group_id,)).fetchall()
    known = {r["agent_id"]: r["concession_balance"] for r in rows}
    return {a: known.get(a, 0.0) for a in agent_ids}


def get_balance(group_id, agent_ids) -> dict[str, float]:
    with connect() as c:
        return _balance_tx(c, group_id, agent_ids)


def get_slots(session_id) -> list[tuple[dt.datetime, dt.datetime]]:
    row = get_session(session_id)
    if row is None:
        return []
    return [(dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b))
            for a, b in json.loads(row["slots_json"])]


def get_history(group_id, limit: int = 50) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute(
            "SELECT * FROM history WHERE group_id = ? "
            "ORDER BY committed_at DESC LIMIT ?", (group_id, limit)).fetchall()


# ---- 전이 -----------------------------------------------------------------

def transition(session_id, expect_state, to_state, **fields) -> bool:
    """조건부 상태 전이.

    Returns:
        True  — 이 호출이 전이를 수행함
        False — 이미 다른 상태였음 (중복 요청). 아무것도 변경하지 않음
    """
    if to_state not in ALL_STATES:
        raise ValueError(f"알 수 없는 상태: {to_state}")

    ts = now()
    sets = ["state = ?", "updated_at = ?"]
    vals: list = [to_state, ts]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        vals.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
    vals += [session_id, expect_state]

    with connect() as c:
        cur = c.execute(
            f"UPDATE sessions SET {', '.join(sets)} "
            f"WHERE session_id = ? AND state = ?", vals)
        if cur.rowcount == 0:
            return False
        c.execute(
            "INSERT INTO transitions (session_id, from_state, to_state, detail, at) "
            "VALUES (?,?,?,?,?)",
            (session_id, expect_state, to_state, fields.get("fail_reason"), ts))
    return True


def save_regret_matrix(session_id, regret, candidate_mask, lam=None) -> bool:
    """regret 행렬을 저장하고 COSTS_COLLECTED 로 전이한다.

    이후 이 행렬은 절대 재계산하지 않는다. 라운드마다 기준선이 바뀌면
    공정성 비교가 무의미해지기 때문이다. 거절 뒤 재제안도 이 행렬과
    같은 lam 으로 다음 슬롯을 고른다.
    """
    return transition(
        session_id, "OPEN", "COSTS_COLLECTED",
        regret_json=json.dumps(regret),
        candidate_mask=json.dumps(sorted(candidate_mask)),
        preferred_set=json.dumps([]),
        lam=lam)


def load_regret(session_id):
    """저장된 regret 행렬을 그대로 반환한다.

    Returns:
        (regret, candidate_mask, preferred_set)
    """
    row = get_session(session_id)
    if not row or not row["regret_json"]:
        return None, set(), set()
    return (json.loads(row["regret_json"]),
            set(json.loads(row["candidate_mask"] or "[]")),
            set(json.loads(row["preferred_set"] or "[]")))


def advance_round(session_id, candidate_mask, preferred_set, round_no) -> None:
    with connect() as c:
        c.execute(
            "UPDATE sessions SET candidate_mask = ?, preferred_set = ?, "
            "round = ?, updated_at = ? WHERE session_id = ?",
            (json.dumps(sorted(candidate_mask)), json.dumps(sorted(preferred_set)),
             round_no, now(), session_id))


# ---- 승인 -----------------------------------------------------------------

def _open_proposal_tx(c, session_id, slot_index, regrets,
                      ttl_min: int | None = None) -> int:
    """새 제안(사람 승인 라운드)을 연다. 호출자의 트랜잭션 안에서만 쓴다.

    proposals 의 UNIQUE(session_id, slot_index) 때문에 이미 시도한 슬롯을
    다시 제안하려 하면 IntegrityError 가 나고 트랜잭션 전체가 롤백된다.
    참가자마다 '발송 대기' 알림 행도 같은 트랜잭션에서 만든다.

    Returns: 새 제안 번호(version)
    """
    ttl = ttl_min if ttl_min is not None else config.APPROVAL_TTL_MIN
    ts = now()
    expires = (dt.datetime.now(dt.timezone.utc)
               + dt.timedelta(minutes=ttl)).isoformat()
    cur = c.execute("SELECT proposal_version FROM sessions WHERE session_id = ?",
                    (session_id,)).fetchone()
    version = cur["proposal_version"] + 1
    c.execute("INSERT INTO proposals (session_id, version, slot_index, status, opened_at) "
              "VALUES (?,?,?,'open',?)", (session_id, version, slot_index, ts))
    c.execute("UPDATE sessions SET proposal_version = ?, proposed_slot = ?, "
              "proposed_regrets = ?, expires_at = ?, updated_at = ? "
              "WHERE session_id = ?",
              (version, slot_index, json.dumps(regrets), expires, ts, session_id))
    for p in c.execute("SELECT agent_id FROM participants WHERE session_id = ?",
                       (session_id,)).fetchall():
        c.execute("INSERT OR IGNORE INTO notifications "
                  "(session_id, proposal_version, agent_id, status, attempts, updated_at) "
                  "VALUES (?,?,?,'pending',0,?)",
                  (session_id, version, p["agent_id"], ts))
    return version


def open_approval_window(session_id, slot_index, regrets,
                         ttl_min: int | None = None) -> bool:
    """협상이 합의한 슬롯으로 1번 제안(1순위)을 연다."""
    ts = now()
    with connect() as c:
        cur = c.execute(
            "UPDATE sessions SET state = 'PENDING_HUMAN_APPROVAL', updated_at = ? "
            "WHERE session_id = ? AND state = 'AGENT_NEGOTIATING'",
            (ts, session_id))
        if cur.rowcount == 0:
            return False
        c.execute(
            "INSERT INTO transitions (session_id, from_state, to_state, detail, at) "
            "VALUES (?, 'AGENT_NEGOTIATING', 'PENDING_HUMAN_APPROVAL', NULL, ?)",
            (session_id, ts))
        _open_proposal_tx(c, session_id, slot_index, regrets, ttl_min)
    return True


def record_approval(session_id, agent_id, verdict,
                    proposal_version: int | None = None) -> str:
    """개별 승인 기록 (현재 제안 버전에 대해서만).

    이 함수는 응답을 '기록'만 한다. 거절이 들어왔을 때 다음 후보로 넘기는
    일은 approval_flow.decide_* 가 한 트랜잭션에서 처리한다.

    Args:
        proposal_version: 주어지면 현재 버전과 같을 때만 기록한다.
    Returns: 'recorded' | 'duplicate' | 'not_pending'
    PRIMARY KEY 제약이 중복 클릭을 자동으로 무시한다.
    """
    if verdict not in ("approve", "reject"):
        raise ValueError(f"잘못된 verdict: {verdict}")

    with connect() as c:
        row = c.execute(
            "SELECT state, expires_at, proposal_version FROM sessions "
            "WHERE session_id = ?", (session_id,)).fetchone()
        if not row or row["state"] != "PENDING_HUMAN_APPROVAL":
            return "not_pending"
        if row["expires_at"] and row["expires_at"] < now():
            return "not_pending"
        if proposal_version is not None and proposal_version != row["proposal_version"]:
            return "not_pending"
        try:
            c.execute(
                "INSERT INTO approvals "
                "(session_id, agent_id, proposal_version, verdict, decided_at) "
                "VALUES (?,?,?,?,?)",
                (session_id, agent_id, row["proposal_version"], verdict, now()))
        except sqlite3.IntegrityError:
            return "duplicate"
    return "recorded"


def get_verdicts(session_id, proposal_version: int | None = None
                 ) -> tuple[set[str], set[str]]:
    """(승인한 agent_id 집합, 거절한 agent_id 집합). 기본은 현재 제안 버전."""
    with connect() as c:
        if proposal_version is None:
            row = c.execute("SELECT proposal_version FROM sessions WHERE session_id = ?",
                            (session_id,)).fetchone()
            proposal_version = row["proposal_version"] if row else 0
        rows = c.execute(
            "SELECT agent_id, verdict FROM approvals "
            "WHERE session_id = ? AND proposal_version = ?",
            (session_id, proposal_version)).fetchall()
    return ({r["agent_id"] for r in rows if r["verdict"] == "approve"},
            {r["agent_id"] for r in rows if r["verdict"] == "reject"})


def slot_effects_of(session_row) -> tuple[set[int], dict[int, float]]:
    """세션에 저장된 회의 조건의 효과: (hard 조건으로 제외된 슬롯, soft 페널티).

    저장된 값을 그대로 읽는다 — 재제안 때 조건을 다시 해석하지 않으므로 처음 협상과
    같은 기준이 유지된다. 조건이 없거나 옛 세션이면 (빈 집합, 빈 dict).
    """
    try:
        raw = session_row["slot_effects_json"]
    except (KeyError, IndexError):
        raw = None
    if not raw:
        return set(), {}
    d = json.loads(raw)
    return ({int(i) for i in d.get("excluded", [])},
            {int(k): float(v) for k, v in d.get("penalty", {}).items()})


def agent_options_of(session_row) -> dict:
    """세션에 저장된 에이전트 옵션 {agent_id: {before_min, after_min, strength}}."""
    try:
        raw = session_row["slot_effects_json"]
    except (KeyError, IndexError):
        raw = None
    if not raw:
        return {}
    return dict(json.loads(raw).get("agent_options") or {})


def approval_tally(session_id) -> tuple[int, int, int]:
    """현재 제안에 대한 (승인 수, 거절 수, 전체 참가자 수).

    이전 제안의 응답은 세지 않는다 — 새 슬롯에는 새 승인이 필요하다.
    """
    with connect() as c:
        rows = c.execute(
            "SELECT a.verdict, COUNT(*) n FROM approvals a "
            "JOIN sessions s ON s.session_id = a.session_id "
            "                AND s.proposal_version = a.proposal_version "
            "WHERE a.session_id = ? GROUP BY a.verdict", (session_id,)).fetchall()
        total = c.execute(
            "SELECT COUNT(*) n FROM participants WHERE session_id = ?",
            (session_id,)).fetchone()["n"]
    counts = {r["verdict"]: r["n"] for r in rows}
    return counts.get("approve", 0), counts.get("reject", 0), total


def clear_approvals(session_id) -> None:
    """이 세션의 모든 승인 기록을 지운다 (관리/테스트용).

    재협상 흐름은 이 함수를 쓰지 않는다. 제안 버전이 바뀌면 이전 응답이
    자동으로 무관해지고, 감사용으로 남는다.
    """
    with connect() as c:
        c.execute("DELETE FROM approvals WHERE session_id = ?", (session_id,))


def get_proposals(session_id) -> list[sqlite3.Row]:
    """시도한 슬롯 목록 (제안 순서대로)."""
    with connect() as c:
        return c.execute("SELECT * FROM proposals WHERE session_id = ? "
                         "ORDER BY version", (session_id,)).fetchall()


def get_attempted_slots(session_id) -> set[int]:
    return {r["slot_index"] for r in get_proposals(session_id)}


def get_notifications(session_id, proposal_version: int | None = None
                      ) -> list[sqlite3.Row]:
    with connect() as c:
        if proposal_version is None:
            return c.execute(
                "SELECT * FROM notifications WHERE session_id = ? "
                "ORDER BY proposal_version, agent_id", (session_id,)).fetchall()
        return c.execute(
            "SELECT * FROM notifications WHERE session_id = ? AND proposal_version = ? "
            "ORDER BY agent_id", (session_id, proposal_version)).fetchall()


def expire_stale_sessions() -> list[str]:
    """TTL 이 지난 승인 대기 세션을 EXPIRED 로 전이한다."""
    ts = now()
    with connect() as c:
        rows = c.execute(
            "SELECT session_id FROM sessions "
            "WHERE state = 'PENDING_HUMAN_APPROVAL' AND expires_at IS NOT NULL "
            "AND expires_at < ?", (ts,)).fetchall()
        ids = [r["session_id"] for r in rows]
        for sid in ids:
            c.execute(
                "UPDATE sessions SET state='EXPIRED', fail_reason='approval timeout', "
                "updated_at=? WHERE session_id=? AND state='PENDING_HUMAN_APPROVAL'",
                (ts, sid))
            c.execute(
                "UPDATE proposals SET status='expired', closed_at=? "
                "WHERE session_id=? AND status='open'", (ts, sid))
            c.execute(
                "INSERT INTO transitions (session_id, from_state, to_state, detail, at) "
                "VALUES (?,'PENDING_HUMAN_APPROVAL','EXPIRED','ttl',?)", (sid, ts))
    return ids


# ---- nonce (replay 방지) ---------------------------------------------------

def check_and_store_nonce(nonce: str) -> bool:
    """처음 보는 nonce 면 저장하고 True. 이미 본 값이면 False."""
    with connect() as c:
        try:
            c.execute("INSERT INTO seen_nonces (nonce, seen_at) VALUES (?,?)",
                      (nonce, now()))
        except sqlite3.IntegrityError:
            return False
    return True


# ---- 잔액 (커밋 성공 후에만) -------------------------------------------------

def apply_balance_once(session_id) -> bool:
    """커밋 성공 후 잔액을 정확히 1회 갱신한다.

    balance_applied 플래그를 같은 트랜잭션에서 확인하고 세팅하므로
    중복 호출되어도 두 번 적용되지 않는다.

    Returns: 실제로 갱신했으면 True
    """
    from common.fairness import update_balance

    with connect() as c:
        row = c.execute(
            "SELECT group_id, proposed_regrets, proposed_slot, slots_json, "
            "       balance_applied, state "
            "FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return False
        if row["state"] != "COMMITTED" or row["balance_applied"]:
            return False

        group_id = row["group_id"]
        regrets = json.loads(row["proposed_regrets"])
        slot_start = json.loads(row["slots_json"])[row["proposed_slot"]][0]

        cur = c.execute(
            "SELECT agent_id, concession_balance FROM ledger WHERE group_id = ?",
            (group_id,)).fetchall()
        balance = {r["agent_id"]: r["concession_balance"] for r in cur}
        for a in regrets:
            balance.setdefault(a, 0.0)

        new_balance = update_balance(balance, regrets)
        ts = now()
        for agent, val in new_balance.items():
            c.execute(
                "INSERT INTO ledger (group_id, agent_id, concession_balance, updated_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(group_id, agent_id) DO UPDATE SET "
                "  concession_balance = excluded.concession_balance, "
                "  updated_at = excluded.updated_at",
                (group_id, agent, val, ts))
        for agent, r in regrets.items():
            c.execute(
                "INSERT INTO history (group_id, session_id, agent_id, slot_start, "
                "regret, balance_after, committed_at) VALUES (?,?,?,?,?,?,?)",
                (group_id, session_id, agent, slot_start, r,
                 new_balance[agent], ts))

        c.execute("UPDATE sessions SET balance_applied = 1, updated_at = ? "
                  "WHERE session_id = ?", (ts, session_id))
    return True


def seed_balance(group_id: str, balances: dict[str, float]) -> None:
    """시연용: 과거 이력을 원장에 미리 주입한다.

    빈 원장으로 시연하면 잔액 보정이 눈에 보이지 않으므로,
    '지난 2회 연속 양보' 상황을 만들어 두는 데 쓴다.
    """
    ts = now()
    with connect() as c:
        for agent, val in balances.items():
            c.execute(
                "INSERT INTO ledger (group_id, agent_id, concession_balance, updated_at) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(group_id, agent_id) DO UPDATE SET "
                "  concession_balance = excluded.concession_balance, "
                "  updated_at = excluded.updated_at",
                (group_id, agent, val, ts))
