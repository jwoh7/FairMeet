"""사람 승인 라운드: 거절 → 차순위 재제안 → 종료 처리.

상태 흐름 (PENDING_HUMAN_APPROVAL 안에서 제안 번호(version)만 올라간다):

    제안 v1 ── 전원 승인 ──────────────▶ finalize (재확인 → 커밋 → 잔액 1회)
       │
       └─ 1명이라도 거절 ─▶ v1 슬롯을 '시도함'으로 기록, v1 링크·응답 무효화
                           │
                           ├─ 남은 후보 있음 & 라운드 여유 있음
                           │     └▶ solve() 로 다음 슬롯 선택 → 제안 v2 (새 링크·새 이메일)
                           ├─ 남은 후보 없음 ─────────────▶ CANDIDATES_EXHAUSTED
                           └─ 최대 승인 라운드 도달 ──────▶ MAX_APPROVAL_ROUNDS_REACHED

거절 처리(기록 → 슬롯 시도 표시 → 다음 슬롯 선택 → 새 제안 개설)는 모두
하나의 BEGIN IMMEDIATE 트랜잭션이다. 그래서
  - 동시에 들어온 승인/거절은 SQLite 쓰기 락으로 직렬화되고,
  - 두 사람이 동시에 거절해도 제안은 한 번만 넘어가며,
  - 중간에 실패하면 아무것도 바뀌지 않는다.
네트워크 I/O(이메일)는 트랜잭션 밖의 dispatch_notifications() 에서만 한다.

트랜잭션 안에서는 store 의 다른 함수를 부르지 않는다. 그 함수들은 새 연결을
열기 때문에 이 트랜잭션이 쥔 락에 막힌다. 반드시 같은 conn 으로 SQL 을 쓴다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import sqlite3
from dataclasses import dataclass, field

from common import config
from common.fairness import solve
from common.slots import format_slot
from mediator import policy as policy_mod
from mediator import store, tokens
from mediator import notifier as notifier_mod
from mediator.notifier import NotificationError, redact

log = logging.getLogger("fairmeet.approval")

PENDING = "PENDING_HUMAN_APPROVAL"

# 사용자에게 '새 협상' 선택지를 보여주는 종료 상태
FOLLOWUP_STATES = frozenset({
    "NO_COMMON_SLOT", "CANDIDATES_EXHAUSTED", "MAX_APPROVAL_ROUNDS_REACHED",
    "NO_FEASIBLE_SLOT", "EXPIRED", "REJECTED",
})

MAX_SEARCH_DAYS = 28
MIN_DURATION_MIN = 30

FOLLOWUP_OPTIONS = [
    {"action": "widen_range", "label": "날짜 범위를 넓혀 새 협상 시작"},
    {"action": "shorten_duration", "label": "회의 시간을 줄여 새 협상 시작"},
    {"action": "close", "label": "이번 협상 종료"},
]

# ---- 콜백 (서버가 연결한다) ----------------------------------------------------

_hooks: dict = {"emit": None, "all_approved": None, "new_round": None}


def configure(*, emit=None, on_all_approved=None, on_new_round=None) -> None:
    """서버가 대시보드 방송, 최종 커밋, 이메일 발송 함수를 연결한다.

    테스트는 이 함수를 부르지 않는 한 어떤 백그라운드 작업도 일어나지 않는다.
    """
    if emit is not None:
        _hooks["emit"] = emit
    if on_all_approved is not None:
        _hooks["all_approved"] = on_all_approved
    if on_new_round is not None:
        _hooks["new_round"] = on_new_round


def reset_hooks() -> None:
    for k in _hooks:
        _hooks[k] = None


def _emit_all(events: list[dict]) -> None:
    fn = _hooks["emit"]
    for ev in events:
        log.info("%s %s", ev.get("type"), {k: v for k, v in ev.items() if k != "type"})
        if fn:
            try:
                fn(ev)
            except Exception:                       # noqa: BLE001 — 방송 실패가 흐름을 막지 않게
                log.exception("emit 실패")


def run_hooks(result: "DecisionResult") -> None:
    """웹/Slack 계층이 결정 처리 뒤에 부른다. 커밋/발송은 트랜잭션 밖에서 일어난다."""
    if result.status == "all_approved" and _hooks["all_approved"]:
        _hooks["all_approved"](result.session_id)
    elif result.status == "reopened" and _hooks["new_round"]:
        _hooks["new_round"](result.session_id)


# ---- 결과 타입 -----------------------------------------------------------------

@dataclass
class DecisionResult:
    """status:
        invalid      만료됐거나 이미 처리됐거나 이전 제안의 요청 (아무것도 바꾸지 않음)
        recorded     동의가 기록됨, 다른 참가자를 기다림
        all_approved 마지막 동의 — 호출자가 finalize 를 예약한다
        reopened     거절 → 다음 슬롯으로 새 제안이 열림 (새 이메일 필요)
        terminated   거절 → 더 제안할 수 없어 종료됨
    """
    status: str
    session_id: str | None = None
    state: str | None = None
    proposal_version: int | None = None


_INVALID = DecisionResult("invalid")


# ---- 내부 유틸 -----------------------------------------------------------------

def _slot_text(session_row, slot_index: int | None) -> str:
    if slot_index is None:
        return "(미정)"
    s, e = json.loads(session_row["slots_json"])[slot_index]
    return format_slot(dt.datetime.fromisoformat(s), dt.datetime.fromisoformat(e))


def _common_slots(regret: dict) -> set[int]:
    """전원이 거부권 없이 가능한 슬롯 (regret 행렬은 협상 내내 불변)."""
    if not regret:
        return set()
    n = len(next(iter(regret.values())))
    return {i for i in range(n) if all(regret[a][i] is not None for a in regret)}


slot_effects_of = store.slot_effects_of


def _candidate_slots(regret: dict, session_row) -> set[int]:
    """제안 가능한 슬롯 = 전원 가능 − hard 조건 위반."""
    excluded, _ = slot_effects_of(session_row)
    return _common_slots(regret) - excluded


def _terminal_reason(state: str, max_rounds: int, common: int) -> str:
    return {
        "CANDIDATES_EXHAUSTED":
            f"제안 가능한 후보 {common}개를 모두 시도했습니다",
        "MAX_APPROVAL_ROUNDS_REACHED":
            f"최대 승인 라운드({max_rounds}회)에 도달했습니다",
    }.get(state, state)


def _close_terminal_tx(c, session_id, from_state, to_state, reason, ts, events):
    """종료 상태로 전이하고 남은 발송/링크를 전부 무효화한다."""
    cur = c.execute(
        "UPDATE sessions SET state = ?, fail_reason = ?, updated_at = ? "
        "WHERE session_id = ? AND state = ?",
        (to_state, reason, ts, session_id, from_state))
    if cur.rowcount == 0:
        return False
    c.execute("INSERT INTO transitions (session_id, from_state, to_state, detail, at) "
              "VALUES (?,?,?,?,?)", (session_id, from_state, to_state, reason, ts))
    c.execute("UPDATE approval_tokens SET revoked_at = ? "
              "WHERE session_id = ? AND used_at IS NULL AND revoked_at IS NULL",
              (ts, session_id))
    c.execute("UPDATE notifications SET status = 'cancelled', updated_at = ? "
              "WHERE session_id = ? AND status IN ('pending', 'failed', 'sending')",
              (ts, session_id))
    events.append({"type": "approval_terminated", "state": to_state, "detail": reason})
    return True


def _advance_tx(c, session_id: str, from_state: str, events: list) -> DecisionResult:
    """직전 제안이 이미 닫힌 상태에서, 다음 제안을 열거나 종료한다.

    다음 슬롯은 처음 협상과 같은 solve()(같은 regret 행렬, 현재 양보 잔액, 같은 lam)로
    '아직 시도하지 않은 공통 가능 슬롯' 중에서 고른다. 재제안에는 에이전트 왕복이
    없다 — 비용은 이미 수집됐고, 이번에는 사람이 결정한다.
    """
    ts = store.now()
    s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    regret = json.loads(s["regret_json"]) if s["regret_json"] else {}
    common = _candidate_slots(regret, s)
    _, penalty = slot_effects_of(s)
    attempted = {r["slot_index"] for r in c.execute(
        "SELECT slot_index FROM proposals WHERE session_id = ?", (session_id,))}
    remaining = common - attempted
    max_rounds = config.MAX_APPROVAL_ROUNDS

    terminal = None
    result = None
    if not remaining:
        terminal = "CANDIDATES_EXHAUSTED"
    elif len(attempted) >= max_rounds:
        terminal = "MAX_APPROVAL_ROUNDS_REACHED"
    else:
        lam = s["lam"]
        if lam is None:
            lam = config.LAMBDA_WORK if s["mode"] == "work" else config.LAMBDA_SOCIAL
        balance = store._balance_tx(c, s["group_id"], list(regret))
        result = solve(regret, balance, remaining, lam=lam, slot_penalty=penalty)
        if result is None:
            terminal = "CANDIDATES_EXHAUSTED"

    if terminal:
        _close_terminal_tx(c, session_id, from_state, terminal,
                           _terminal_reason(terminal, max_rounds, len(common)),
                           ts, events)
        return DecisionResult("terminated", session_id, terminal,
                              s["proposal_version"])

    version = store._open_proposal_tx(c, session_id, result.slot_index,
                                      result.regrets)
    if from_state != PENDING:
        c.execute("UPDATE sessions SET state = ?, updated_at = ? "
                  "WHERE session_id = ? AND state = ?",
                  (PENDING, ts, session_id, from_state))
    c.execute("INSERT INTO transitions (session_id, from_state, to_state, detail, at) "
              "VALUES (?,?,?,?,?)",
              (session_id, from_state, PENDING, f"proposal {version} opened", ts))
    s2 = c.execute("SELECT slots_json FROM sessions WHERE session_id = ?",
                   (session_id,)).fetchone()
    events.append({"type": "reproposed", "round": version,
                   "slot": _slot_text(s2, result.slot_index),
                   "remaining": len(remaining) - 1})
    return DecisionResult("reopened", session_id, PENDING, version)


# ---- 결정 처리 -----------------------------------------------------------------

def _decide_tx(c, session_id, version, agent_id, verdict, jti, events) -> DecisionResult:
    """승인/거절 한 건. 호출자의 트랜잭션 안에서 실행된다."""
    ts = store.now()
    s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if s is None or s["state"] != PENDING:
        return _INVALID
    if s["proposal_version"] != version:
        return _INVALID                       # 이전 제안의 링크/버튼
    if s["expires_at"] and s["expires_at"] < ts:
        return _INVALID
    prop = c.execute("SELECT status FROM proposals WHERE session_id = ? AND version = ?",
                     (session_id, version)).fetchone()
    if prop is None or prop["status"] != "open":
        return _INVALID
    if c.execute("SELECT 1 FROM participants WHERE session_id = ? AND agent_id = ?",
                 (session_id, agent_id)).fetchone() is None:
        return _INVALID

    if jti is not None:
        # 1회용: 사용/폐기되지 않은 토큰만 소비할 수 있다. rowcount 가 곧 검증이다.
        used = c.execute(
            "UPDATE approval_tokens SET used_at = ? "
            "WHERE jti = ? AND session_id = ? AND proposal_version = ? AND agent_id = ? "
            "  AND used_at IS NULL AND revoked_at IS NULL",
            (ts, jti, session_id, version, agent_id))
        if used.rowcount == 0:
            return _INVALID

    try:
        c.execute("INSERT INTO approvals "
                  "(session_id, agent_id, proposal_version, verdict, decided_at) "
                  "VALUES (?,?,?,?,?)", (session_id, agent_id, version, verdict, ts))
    except sqlite3.IntegrityError:
        return _INVALID                       # 같은 제안에 이미 응답함

    # ---- 승인 정책으로 이 제안의 운명을 판정한다 (기본 = 전원 승인) ----
    policy = policy_mod.ApprovalPolicy.from_row(s)
    pids = [r["agent_id"] for r in c.execute(
        "SELECT agent_id FROM participants WHERE session_id = ?", (session_id,))]
    approved, rejected = set(), set()
    for r in c.execute("SELECT agent_id, verdict FROM approvals "
                       "WHERE session_id = ? AND proposal_version = ?",
                       (session_id, version)):
        (approved if r["verdict"] == "approve" else rejected).add(r["agent_id"])
    outcome = policy_mod.evaluate(policy, pids, approved, rejected)

    if outcome == "approved":
        c.execute("UPDATE proposals SET status = 'approved', closed_at = ? "
                  "WHERE session_id = ? AND version = ?", (ts, session_id, version))
        # 정족수만 채우고 끝난 경우, 아직 응답하지 않은 사람의 링크·발송 대기를 닫는다.
        _close_open_links_tx(c, session_id, version, ts)
        events.append({"type": "approvals_complete", "round": version})
        return DecisionResult("all_approved", session_id, PENDING, version)
    if outcome == "pending":
        return DecisionResult("recorded", session_id, PENDING, version)

    # ---- 필수 참석자의 거절, 또는 정족수 달성 불가: 이 제안을 닫고 다음으로 ----
    c.execute("UPDATE proposals SET status = 'rejected', closed_at = ? "
              "WHERE session_id = ? AND version = ?", (ts, session_id, version))
    _close_open_links_tx(c, session_id, version, ts)
    # 누가 거절했는지는 이벤트/로그에 싣지 않는다.
    events.append({"type": "proposal_rejected", "round": version})
    return _advance_tx(c, session_id, PENDING, events)


def _close_open_links_tx(c, session_id: str, version: int, ts: str) -> None:
    """이 제안의 미사용 링크를 폐기하고 아직 나가지 않은 이메일을 취소한다."""
    c.execute("UPDATE approval_tokens SET revoked_at = ? "
              "WHERE session_id = ? AND proposal_version = ? "
              "  AND used_at IS NULL AND revoked_at IS NULL", (ts, session_id, version))
    c.execute("UPDATE notifications SET status = 'cancelled', updated_at = ? "
              "WHERE session_id = ? AND proposal_version = ? "
              "  AND status IN ('pending', 'failed', 'sending')",
              (ts, session_id, version))


def _after_decision(res: "DecisionResult") -> None:
    """재협상(일정 변경)으로 만든 세션이 종료 상태가 되면 변경 요청에 알린다. 다른 세션은 무관."""
    if res.status == "terminated" and res.session_id:
        try:
            from mediator import change_flow
            change_flow.reconcile_for_session(res.session_id)
        except Exception:                                        # noqa: BLE001
            log.exception("일정 변경 결과 반영 실패")


def decide_with_token(token: str, verdict: str, now: float | None = None
                      ) -> DecisionResult:
    """이메일 링크의 토큰으로 승인/거절을 처리한다. 결과는 항상 멱등이다.

    서명·만료가 틀리거나, 이전 제안의 링크이거나, 이미 쓴 링크면 invalid 를
    돌려주고 아무것도 바꾸지 않는다.
    """
    if verdict not in ("approve", "reject"):
        raise ValueError(f"잘못된 verdict: {verdict}")
    try:
        claims = tokens.verify(token, now)
    except tokens.TokenInvalid:
        return _INVALID
    events: list[dict] = []
    with store.connect() as c:
        res = _decide_tx(c, claims.session_id, claims.proposal_version,
                         claims.agent_id, verdict, claims.jti, events)
    _emit_all(events)
    _after_decision(res)
    return res


def decide_for_agent(session_id: str, agent_id: str, verdict: str,
                     proposal_version: int | None = None) -> DecisionResult:
    """토큰 없이 참가자를 직접 지정해 결정한다 (Slack 버튼, 데모, 테스트).

    proposal_version 을 주면 그 제안에 대한 응답으로만 인정한다.
    """
    if verdict not in ("approve", "reject"):
        raise ValueError(f"잘못된 verdict: {verdict}")
    events: list[dict] = []
    with store.connect() as c:
        row = c.execute("SELECT proposal_version FROM sessions WHERE session_id = ?",
                        (session_id,)).fetchone()
        if row is None:
            return _INVALID
        version = row["proposal_version"] if proposal_version is None else proposal_version
        res = _decide_tx(c, session_id, version, agent_id, verdict, None, events)
    _emit_all(events)
    _after_decision(res)
    return res


def settle_expired_sessions() -> dict:
    """응답 기한이 지난 승인 대기 세션을 '한 번만' 마무리한다.

    기한이 지나면 더 기다릴 이유가 없으므로, 그때까지 모인 응답으로 정족수·필수 참석자 기준을
    다시 검증한다 (정족수는 초대된 전체 인원 기준이다):
      - 기준 충족  → 이 제안을 승인으로 닫고 확정 절차(재확인 → 커밋)로 넘긴다
      - 미충족     → 기존과 같이 EXPIRED 로 종료한다 (이벤트·잔액 변화 없음)

    조건부 UPDATE 라 여러 번 불려도 세션마다 한 번만 결정된다.
    Returns: {"finalize": [session_id...], "expired": [session_id...]}
    """
    ts = store.now()
    out: dict = {"finalize": [], "expired": []}
    events: list[dict] = []
    with store.connect() as c:
        rows = c.execute(
            "SELECT * FROM sessions WHERE state = ? AND expires_at IS NOT NULL "
            "AND expires_at < ?", (PENDING, ts)).fetchall()
        for s in rows:
            sid, version = s["session_id"], s["proposal_version"]
            pids = [r["agent_id"] for r in c.execute(
                "SELECT agent_id FROM participants WHERE session_id = ?", (sid,))]
            approved, rejected = set(), set()
            for r in c.execute("SELECT agent_id, verdict FROM approvals "
                               "WHERE session_id = ? AND proposal_version = ?", (sid, version)):
                (approved if r["verdict"] == "approve" else rejected).add(r["agent_id"])
            pol = policy_mod.ApprovalPolicy.from_row(s)
            if policy_mod.settle_at_deadline(pol, pids, approved, rejected) == "approved":
                got = c.execute("UPDATE proposals SET status = 'approved', closed_at = ? "
                                "WHERE session_id = ? AND version = ? AND status = 'open'",
                                (ts, sid, version))
                if got.rowcount:
                    _close_open_links_tx(c, sid, version, ts)
                    c.execute("INSERT INTO transitions (session_id, from_state, to_state, "
                              "detail, at) VALUES (?,?,?,?,?)",
                              (sid, PENDING, PENDING, "deadline reached: quorum satisfied", ts))
                    events.append({"type": "approvals_complete", "round": version})
                    out["finalize"].append(sid)
                continue
            cur = c.execute(
                "UPDATE sessions SET state = 'EXPIRED', fail_reason = 'approval timeout', "
                "updated_at = ? WHERE session_id = ? AND state = ?", (ts, sid, PENDING))
            if cur.rowcount:
                c.execute("UPDATE proposals SET status = 'expired', closed_at = ? "
                          "WHERE session_id = ? AND status = 'open'", (ts, sid))
                _close_open_links_tx(c, sid, version, ts)
                c.execute("INSERT INTO transitions (session_id, from_state, to_state, detail, at) "
                          "VALUES (?,?,'EXPIRED','ttl',?)", (sid, PENDING, ts))
                events.append({"type": "approval_terminated", "state": "EXPIRED",
                               "detail": "승인 시간이 지났습니다"})
                out["expired"].append(sid)
    _emit_all(events)
    for sid in out["finalize"]:               # 커밋은 트랜잭션 밖에서 (기존 확정 경로와 같다)
        if _hooks["all_approved"]:
            _hooks["all_approved"](sid)
    return out


def recover_from_stale_conflict(session_id: str) -> DecisionResult:
    """승인 대기 중 캘린더 충돌(STALE_CONFLICT)이 확인된 세션을 다음 후보로 넘긴다.

    fail-closed 는 그대로다: 충돌 슬롯에는 이벤트가 만들어지지 않았고, 새 슬롯도
    전원 재승인 + 커밋 직전 재확인을 다시 통과해야 한다. 충돌 슬롯은 '시도함'으로
    남으므로 무한 재시도 방지 규칙(후보 소진/최대 라운드)이 그대로 적용된다.
    """
    events: list[dict] = []
    ts = store.now()
    with store.connect() as c:
        s = c.execute("SELECT state, proposal_version FROM sessions WHERE session_id = ?",
                      (session_id,)).fetchone()
        if s is None or s["state"] != "STALE_CONFLICT":
            return _INVALID                    # 이미 처리됨 (멱등)
        c.execute("UPDATE proposals SET status = 'conflict', closed_at = ? "
                  "WHERE session_id = ? AND version = ?",
                  (ts, session_id, s["proposal_version"]))
        c.execute("UPDATE approval_tokens SET revoked_at = ? "
                  "WHERE session_id = ? AND used_at IS NULL AND revoked_at IS NULL",
                  (ts, session_id))
        events.append({"type": "proposal_conflict", "round": s["proposal_version"]})
        res = _advance_tx(c, session_id, "STALE_CONFLICT", events)
    _emit_all(events)
    _after_decision(res)
    return res


# ---- 승인 페이지용 조회 ----------------------------------------------------------

@dataclass
class LinkView:
    session_id: str
    display_name: str
    slot_text: str
    expires_at: dt.datetime
    proposal_version: int
    role_text: str = ""            # 전원 승인이 아닐 때만 채워진다
    policy_text: str = ""


def inspect_token(token: str, now: float | None = None) -> LinkView | None:
    """GET 용. 링크가 지금 사용 가능한지만 확인하고 아무것도 바꾸지 않는다.

    None 이면 화면에는 항상 같은 '만료되었거나 이미 처리된 요청' 문구가 나간다.
    """
    try:
        claims = tokens.verify(token, now)
    except tokens.TokenInvalid:
        return None
    with store.connect() as c:
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?",
                      (claims.session_id,)).fetchone()
        if (s is None or s["state"] != PENDING
                or s["proposal_version"] != claims.proposal_version
                or (s["expires_at"] and s["expires_at"] < store.now())):
            return None
        tok = c.execute(
            "SELECT 1 FROM approval_tokens WHERE jti = ? AND session_id = ? "
            "AND proposal_version = ? AND agent_id = ? "
            "AND used_at IS NULL AND revoked_at IS NULL",
            (claims.jti, claims.session_id, claims.proposal_version,
             claims.agent_id)).fetchone()
        prop = c.execute("SELECT status FROM proposals WHERE session_id = ? AND version = ?",
                         (claims.session_id, claims.proposal_version)).fetchone()
        p = c.execute("SELECT display_name FROM participants "
                      "WHERE session_id = ? AND agent_id = ?",
                      (claims.session_id, claims.agent_id)).fetchone()
        done = c.execute("SELECT 1 FROM approvals WHERE session_id = ? "
                         "AND proposal_version = ? AND agent_id = ?",
                         (claims.session_id, claims.proposal_version,
                          claims.agent_id)).fetchone()
        if tok is None or prop is None or prop["status"] != "open" or p is None or done:
            return None
        pol = policy_mod.ApprovalPolicy.from_row(s)
        role = policy = ""
        if pol.mode != "all":
            names = {r["agent_id"]: r["display_name"] for r in c.execute(
                "SELECT agent_id, display_name FROM participants WHERE session_id = ?",
                (claims.session_id,))}
            role = policy_mod.role_text(pol, claims.agent_id)
            policy = policy_mod.describe(pol, len(names), names)
        return LinkView(
            session_id=claims.session_id, display_name=p["display_name"],
            slot_text=_slot_text(s, s["proposed_slot"]),
            expires_at=dt.datetime.fromisoformat(s["expires_at"]).astimezone(),
            proposal_version=claims.proposal_version,
            role_text=role, policy_text=policy)


# ---- 이메일 발송 -----------------------------------------------------------------

@dataclass
class DispatchSummary:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)   # [(표시 이름, 오류 메시지)]

    @property
    def attempted(self) -> int:
        return self.sent + self.failed


def _safe_error(exc: Exception) -> str:
    """관리자 화면에 보일 오류 문자열. 비밀번호가 섞여 있어도 지운다."""
    text = str(exc) if isinstance(exc, NotificationError) \
        else f"{type(exc).__name__}: {exc}"
    return redact(text, os.environ.get("FAIRMEET_SMTP_PASSWORD", ""))[:300]


def _is_current(session_id: str, version: int) -> bool:
    row = store.get_session(session_id)
    return bool(row and row["state"] == PENDING and row["proposal_version"] == version
                and not (row["expires_at"] and row["expires_at"] < store.now()))


STUCK_SENDING_AFTER = dt.timedelta(minutes=5)


def dispatch_notifications(session_id: str, notifier=None) -> DispatchSummary:
    """현재 제안에 대한 승인 요청 이메일을 보낸다 (재시도 겸용).

    - 종료/만료된 세션, 또는 이미 다른 제안으로 넘어간 경우에는 아무것도 보내지 않는다.
    - 이미 '발송됨'인 참가자에게는 다시 보내지 않는다. '발송 실패'/'대기'만 대상이다.
    - 발송할 때마다 새 토큰을 발급하고 같은 참가자의 이전 미사용 토큰은 폐기한다.
    - 실패하면 세션을 성공처럼 두지 않고 notifications 에 오류를 남긴다.
      세션 상태는 승인 대기 그대로여서, 재시도로 이어서 진행할 수 있다.
    """
    summary = DispatchSummary()
    ts = store.now()
    stale = (dt.datetime.now(dt.timezone.utc) - STUCK_SENDING_AFTER).isoformat()

    # 1) 발송 대상을 원자적으로 선점하고 토큰을 발급한다 (동시 호출이 겹쳐도 한 번만).
    claimed = []
    with store.connect() as c:
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if (s is None or s["state"] != PENDING
                or (s["expires_at"] and s["expires_at"] < ts)):
            return summary
        version = s["proposal_version"]
        slot_text = _slot_text(s, s["proposed_slot"])
        exp_epoch = int(dt.datetime.fromisoformat(s["expires_at"]).timestamp())
        pol = policy_mod.ApprovalPolicy.from_row(s)
        names = {r["agent_id"]: r["display_name"] for r in c.execute(
            "SELECT agent_id, display_name FROM participants WHERE session_id = ?",
            (session_id,))}
        pol_text = (None if pol.mode == "all"
                    else policy_mod.describe(pol, len(names), names))
        change_note = (
            "이 요청은 이미 확정된 일정을 바꾸기 위한 재협상입니다. 동의하지 않거나 새 시간이 "
            "확정되지 않으면 기존 일정이 그대로 유지됩니다."
            if s["change_request_id"] else None)
        rows = c.execute(
            "SELECT n.agent_id, n.status, p.display_name, p.email "
            "FROM notifications n JOIN participants p "
            "  ON p.session_id = n.session_id AND p.agent_id = n.agent_id "
            "WHERE n.session_id = ? AND n.proposal_version = ? "
            "  AND (n.status IN ('pending', 'failed') "
            "       OR (n.status = 'sending' AND n.updated_at < ?)) "
            "ORDER BY n.agent_id", (session_id, version, stale)).fetchall()
        for r in rows:
            got = c.execute(
                "UPDATE notifications SET status = 'sending', attempts = attempts + 1, "
                "updated_at = ? WHERE session_id = ? AND proposal_version = ? "
                "AND agent_id = ? AND status = ?",
                (ts, session_id, version, r["agent_id"], r["status"]))
            if got.rowcount == 0:
                continue
            c.execute("UPDATE approval_tokens SET revoked_at = ? "
                      "WHERE session_id = ? AND proposal_version = ? AND agent_id = ? "
                      "AND used_at IS NULL AND revoked_at IS NULL",
                      (ts, session_id, version, r["agent_id"]))
            token, jti = tokens.mint(session_id, version, r["agent_id"], exp_epoch)
            c.execute("INSERT INTO approval_tokens "
                      "(jti, session_id, proposal_version, agent_id, expires_at, created_at) "
                      "VALUES (?,?,?,?,?,?)",
                      (jti, session_id, version, r["agent_id"], s["expires_at"], ts))
            claimed.append((r["agent_id"], r["display_name"], r["email"], token))

    if not claimed:
        return summary

    # 2) 발송 (트랜잭션 밖). notifier 설정 자체가 틀렸으면 전부 '발송 실패'로 남긴다.
    config_error = None
    try:
        notifier = notifier or notifier_mod.get_notifier()
    except NotificationError as exc:
        config_error = _safe_error(exc)
    backend = getattr(notifier, "name", "unknown") if notifier else "unconfigured"

    events = []
    for agent_id, name, email, token in claimed:
        err = config_error
        if err is None and not _is_current(session_id, version):
            _mark_notification(session_id, version, agent_id, "cancelled", None, backend)
            summary.skipped += 1
            continue
        if err is None:
            try:
                notifier.send(notifier_mod.render_approval_message(
                    to_email=email, to_name=name, slot_text=slot_text,
                    url=notifier_mod.approval_url(token), expires_at=exp_epoch,
                    session_id=session_id, proposal_version=version,
                    agent_id=agent_id, policy_text=pol_text,
                    role_text=(None if pol.mode == "all"
                               else policy_mod.role_text(pol, agent_id)),
                    change_note=change_note))
            except Exception as exc:               # noqa: BLE001 — 어떤 실패든 기록한다
                err = _safe_error(exc)

        if err is None:
            _mark_notification(session_id, version, agent_id, "sent", None, backend)
            summary.sent += 1
        else:
            _mark_notification(session_id, version, agent_id, "failed", err, backend)
            summary.failed += 1
            summary.errors.append((name, err))
            events.append({"type": "notification_failed", "who": name, "detail": err})
    events.append({"type": "notifications", "round": version, "sent": summary.sent,
                   "failed": summary.failed, "backend": backend})
    _emit_all(events)
    return summary


def _mark_notification(session_id, version, agent_id, status, error, backend) -> None:
    ts = store.now()
    with store.connect() as c:
        c.execute(
            "UPDATE notifications SET status = ?, last_error = ?, backend = ?, "
            "updated_at = ?, sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END "
            "WHERE session_id = ? AND proposal_version = ? AND agent_id = ? "
            "AND status = 'sending'",
            (status, error, backend, ts, status, ts, session_id, version, agent_id))


# ---- 대시보드용 뷰 (링크/토큰은 절대 포함하지 않는다) -----------------------------------

_STATUS_LABEL = {"sent": "발송됨", "responded": "응답 완료",
                 "send_failed": "발송 실패", "pending": "발송 대기"}

_OUTCOMES = {
    "NO_COMMON_SLOT": ("모든 참가자가 함께 가능한 시간이 없습니다",
                       "이번 조건에서는 전원이 동시에 가능한 시간을 찾지 못했습니다."),
    "CANDIDATES_EXHAUSTED": ("제안할 수 있는 시간을 모두 시도했습니다",
                             "가능한 후보가 모두 거절되었거나 제외되었습니다."),
    "MAX_APPROVAL_ROUNDS_REACHED": ("최대 승인 횟수에 도달했습니다",
                                    "정해 둔 횟수만큼 제안했지만 전원이 동의한 시간이 없습니다."),
    "NO_FEASIBLE_SLOT": ("합의할 수 있는 시간을 찾지 못했습니다",
                         "에이전트 협상 단계에서 서로 맞는 시간이 나오지 않았습니다."),
    "EXPIRED": ("승인 시간이 지났습니다",
                "정해진 시간 안에 전원의 응답을 받지 못했습니다."),
    "REJECTED": ("제안이 거절되었습니다",
                 "이 협상은 재협상 기능 도입 이전에 거절로 종료되었습니다."),
}

_INFRA_OUTCOMES = {
    "AGENT_UNAVAILABLE": ("참가자의 캘린더나 에이전트에 연결하지 못했습니다",
                          "안전을 위해 일정을 만들지 않고 중단했습니다."),
    "COMMIT_FAILED": ("일정을 등록하지 못했습니다",
                      "캘린더에 이벤트가 만들어지지 않았고 양보 잔액도 바뀌지 않았습니다."),
}


def outcome_for(state: str) -> dict | None:
    """종료 상태를 사용자 언어로 설명하고 다음 선택지를 붙인다."""
    if state in _OUTCOMES:
        title, msg = _OUTCOMES[state]
        return {"title": title, "message": msg, "options": FOLLOWUP_OPTIONS}
    if state in _INFRA_OUTCOMES:
        title, msg = _INFRA_OUTCOMES[state]
        return {"title": title, "message": msg,
                "options": [o for o in FOLLOWUP_OPTIONS if o["action"] == "close"]}
    return None


def _timeline_text(proposals, terminal_state: str | None) -> str:
    parts = []
    for i, p in enumerate(proposals, 1):
        st = p["status"]
        if st == "rejected":
            parts.append(f"{i}순위 거절")
        elif st == "conflict":
            parts.append(f"{i}순위 일정 충돌")
        elif st == "expired":
            parts.append(f"{i}순위 시간 초과")
        elif st == "approved":
            parts.append(f"{i}순위 전원 승인")
        else:
            parts.append("1순위 제안" if i == 1 else f"{i}순위 재제안")
    if terminal_state in ("NO_COMMON_SLOT",):
        parts.append("공통 시간 없음")
    elif terminal_state == "CANDIDATES_EXHAUSTED":
        parts.append("후보 소진")
    elif terminal_state == "MAX_APPROVAL_ROUNDS_REACHED":
        parts.append("최대 라운드 도달")
    return " → ".join(parts)


def _conditions_view(s) -> dict | None:
    """자연어로 확인한 회의 조건 요약. 조건이 없는 세션(수동 협상, 옛 세션)은 None."""
    try:
        raw = s["constraints_json"]
    except (KeyError, IndexError):
        return None
    if not raw:
        return None
    d = json.loads(raw)
    try:
        manual = json.loads(s["slot_effects_json"] or "{}").get("manual_checks", [])
    except ValueError:
        manual = []
    return {
        "title": d.get("title"), "purpose": d.get("purpose"),
        "durationMin": d.get("duration_min"),
        "items": [{"type": c["type"], "strength": c["strength"], "priority": c["priority"],
                   "sourceText": c.get("source_text", ""), "explanation": c.get("explanation", ""),
                   "supported": c.get("supported", True),
                   "acknowledged": c.get("acknowledged", False)}
                  for c in d.get("constraints", []) if c.get("status") != "removed"],
        "manualChecks": manual,
    }


def session_view(session_id: str) -> dict | None:
    """관리자 대시보드가 그리는 세션 요약.

    담지 않는 것: 승인 링크/토큰, 개인별 regret, 누가 거절했는지, 참가자 이메일.
    '응답 완료'는 응답했다는 사실만 보여주고 동의/거절 여부는 구분하지 않는다.
    """
    s = store.get_session(session_id)
    if s is None:
        return None
    parts = store.get_participants(session_id)
    proposals = store.get_proposals(session_id)
    version = s["proposal_version"]
    state = s["state"]
    pending = state == PENDING

    responded: set[str] = set()
    notif = {}
    if version:
        with store.connect() as c:
            responded = {r["agent_id"] for r in c.execute(
                "SELECT agent_id FROM approvals WHERE session_id = ? "
                "AND proposal_version = ?", (session_id, version))}
        notif = {n["agent_id"]: n for n in store.get_notifications(session_id, version)}

    people = []
    counts = {"sent": 0, "send_failed": 0, "pending": 0, "responded": 0}
    for p in parts:
        n = notif.get(p["agent_id"])
        if not pending:
            status = None
        elif p["agent_id"] in responded:
            status = "responded"
        elif n is None or n["status"] in ("pending", "sending", "cancelled"):
            status = "pending"
        elif n["status"] == "failed":
            status = "send_failed"
        else:
            status = "sent"
        if status:
            counts[status] += 1
        people.append({
            "name": p["display_name"], "status": status,
            "label": _STATUS_LABEL.get(status, ""),
            "error": (n["last_error"] if status == "send_failed" and n else None)})

    if not pending:
        n_status = "none"
    elif counts["send_failed"] and counts["send_failed"] == len(parts):
        n_status = "all_failed"
    elif counts["send_failed"]:
        n_status = "partial_failure"
    elif counts["pending"]:
        n_status = "pending"
    else:
        n_status = "ok"

    regret = json.loads(s["regret_json"]) if s["regret_json"] else {}
    common = len(_candidate_slots(regret, s))
    terminal_state = state if state in store.TERMINAL_STATES else None

    pol = policy_mod.ApprovalPolicy.from_row(s)
    names = {p["agent_id"]: p["display_name"] for p in parts}
    policy_view = {
        "mode": pol.mode,
        "needed": pol.needed(len(parts)),
        "total": len(parts),
        "required": ([names.get(a, "") for a in pol.required]
                     if pol.mode != "all" else []),
        "description": policy_mod.describe(pol, len(parts), names),
    }

    return {
        "sessionId": session_id,
        "state": state,
        "policy": policy_view,
        "conditions": _conditions_view(s),
        "terminal": terminal_state is not None,
        "reason": s["fail_reason"],
        "mode": s["mode"], "durationMin": s["duration_min"],
        "currentSlot": _slot_text(s, s["proposed_slot"]) if s["proposed_slot"] is not None else None,
        "round": {"current": version, "max": config.MAX_APPROVAL_ROUNDS,
                  "candidates": common, "attempted": len(proposals)},
        "timeline": [{"rank": p["version"], "status": p["status"],
                      "slot": _slot_text(s, p["slot_index"])} for p in proposals],
        "timelineText": _timeline_text(proposals, terminal_state),
        "participants": people,
        "notification": {
            "status": n_status, "failed": counts["send_failed"],
            "sent": counts["sent"] + counts["responded"],
            "retryable": pending and counts["send_failed"] + counts["pending"] > 0,
            "backend": next((n["backend"] for n in notif.values() if n["backend"]), None)
                       or config.EMAIL_BACKEND,
            "warnings": notifier_mod.base_url_warnings(),
        },
        "outcome": outcome_for(state) if terminal_state else None,
        "eventId": s["google_event_id"],
    }


# ---- 종료 후 선택지 ----------------------------------------------------------------

def followup_plan(session_id: str, action: str) -> dict:
    """'날짜 범위 넓히기 / 회의 시간 줄이기'로 새 협상을 시작할 조건을 계산한다.

    Raises:
        LookupError: 세션 없음
        ValueError:  이 상태에서는 선택할 수 없거나 더 조정할 수 없음 (사용자에게 보일 문장)
    """
    s = store.get_session(session_id)
    if s is None:
        raise LookupError("세션을 찾을 수 없습니다")
    if s["state"] not in FOLLOWUP_STATES:
        raise ValueError("이 협상은 새 협상을 제안할 수 있는 종료 상태가 아닙니다")

    slots = store.get_slots(session_id)
    span = ((slots[-1][0].date() - slots[0][0].date()).days + 1) if slots else 7
    days = 7 * max(1, math.ceil(span / 7))
    duration = s["duration_min"]

    if action == "widen_range":
        if days >= MAX_SEARCH_DAYS:
            raise ValueError(f"탐색 범위를 더 넓힐 수 없습니다 (최대 {MAX_SEARCH_DAYS}일)")
        days += 7
    elif action == "shorten_duration":
        if duration <= MIN_DURATION_MIN:
            raise ValueError(f"회의 시간을 더 줄일 수 없습니다 (최소 {MIN_DURATION_MIN}분)")
        duration = max(MIN_DURATION_MIN, duration - 30)
    else:
        raise ValueError("알 수 없는 선택입니다")
    return {"mode": s["mode"], "durationMin": duration, "days": days,
            "groupId": s["group_id"]}


def close_session(session_id: str) -> bool:
    """'이번 협상 종료'. 종료 상태는 이미 확정이므로 상태는 바꾸지 않고 감사 기록만 남긴다."""
    s = store.get_session(session_id)
    if s is None or s["state"] not in store.TERMINAL_STATES:
        return False
    ts = store.now()
    with store.connect() as c:
        c.execute("INSERT INTO transitions (session_id, from_state, to_state, detail, at) "
                  "VALUES (?,?,?,?,?)",
                  (session_id, s["state"], s["state"], "closed by organizer", ts))
    return True
