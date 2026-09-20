"""확정된 회의의 일정 변경: 변경 요청 → 다른 참가자 투표 → 적용(취소 / 제외 진행 / 재협상).

원칙
  - 변경 요청 '즉시' Google 이벤트를 고치거나 지우지 않는다. 투표가 통과한 뒤에만 적용한다.
  - 요청·투표 링크는 서명·만료·일회용 토큰이다 (tokens.mint_change). 승인 토큰과 키가 다르다.
  - 사유를 받는 필드가 없다. 요청자의 신원도 메일·화면에 싣지 않는다.
  - 결정 규칙은 원래 회의를 확정한 승인 정책(전원 / 정족수 + 필수 참석자)과 같다. 요청자는
    변경에 찬성한 것으로 센다. 투표자는 '실제로 참석하기로 한 사람'(그 회의를 승인한 사람)이다.
  - 적용이 실패하거나 거절되면 원래 일정은 그대로다. 같은 이벤트를 고치므로 중복 이벤트가 없다.
  - 세션마다 진행 중인 변경 요청은 하나뿐이다(DB 부분 UNIQUE 인덱스). 동시에 들어오면 하나만
    성공하고 나머지는 '이미 진행 중'으로 거절된다.
  - 모든 상태 전이는 조건부 UPDATE 라 중복 클릭·재시도에도 한 번만 일어난다.

변경 종류
  renegotiate      모두의 새 시간을 다시 협상한다 (통과 뒤 새 협상 세션 → 확정되면 같은 이벤트 수정)
  proceed_without  요청자를 뺀 채 기존 시간에 진행한다 (필수 참석자가 아니고 정족수가 유지될 때만)
  cancel           회의를 취소한다

누가 무엇을 정하는가 (v5)
  참가자   '일정 변경 요청 보내기'만 한다 (submit_intake). 처리 방법을 고르지 않고 사유도 쓰지 않는다.
           요청은 일정을 바꾸지 않고 다른 참가자에게 아무것도 보내지 않는다.
  주최자   관리자 화면에서 처리 방법을 고른다 (decide_intake): 재협상 / 요청자 제외 진행 / 취소 제안 / 거절(유지).
           재협상·제외·취소를 고르면 그때 비로소 위의 투표(change_requests)가 열리고 투표 메일이 나간다.
  처리 이력은 change_events 에 남는다 (누가 무엇을 언제 했는지만, 사유는 없다).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import sqlite3
from dataclasses import dataclass, field

from common import config
from common.ids import new_session_id
from common.slots import KST, build_slots, format_slot
from mediator import approval_flow, store, tokens
from mediator import notifier as notifier_mod
from mediator import policy as policy_mod
from mediator.approval_flow import DispatchSummary, _safe_error
from mediator.notifier import NotificationError

log = logging.getLogger("fairmeet.change")

KINDS = ("renegotiate", "proceed_without", "cancel")
OPEN_STATES = ("voting", "applying", "replanning")
KIND_LABEL = {"renegotiate": "새 시간 재협상", "proceed_without": "요청자를 제외하고 기존 시간에 진행",
              "cancel": "회의 취소"}
STATE_LABEL = {"voting": "투표 진행 중", "denied": "거절됨 (기존 일정 유지)",
               "expired": "시간 초과 (기존 일정 유지)", "applying": "적용 중",
               "replanning": "새 시간 재협상 중", "applied": "적용 완료",
               "failed": "실패 (기존 일정 유지)"}
STUCK_APPLYING_AFTER = dt.timedelta(minutes=2)


class ChangeError(Exception):
    """사용자에게 그대로 보일 수 있는 거절 사유."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message = message
        self.status = status


# ---- 콜백 (서버가 연결한다) ---------------------------------------------------------------------

_hooks: dict = {"emit": None, "on_created": None, "on_accepted": None, "on_settled": None}


def configure(*, emit=None, on_created=None, on_accepted=None, on_settled=None) -> None:
    for k, v in (("emit", emit), ("on_created", on_created),
                 ("on_accepted", on_accepted), ("on_settled", on_settled)):
        if v is not None:
            _hooks[k] = v


def reset_hooks() -> None:
    for k in _hooks:
        _hooks[k] = None


def _emit(events: list[dict]) -> None:
    fn = _hooks["emit"]
    for ev in events:
        log.info("%s %s", ev.get("type"), {k: v for k, v in ev.items() if k != "type"})
        if fn:
            try:
                fn(ev)
            except Exception:                                    # noqa: BLE001
                log.exception("emit 실패")


def _now(now: dt.datetime | None) -> dt.datetime:
    return now or dt.datetime.now(KST)


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat()


# ---- 공통 조회 -----------------------------------------------------------------------------------

def _slot(s) -> tuple[dt.datetime, dt.datetime]:
    a, b = json.loads(s["slots_json"])[s["proposed_slot"]]
    return dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b)


def _withdrawn(c, s) -> set[str]:
    """'요청자를 제외하고 진행'이 적용되어 이미 빠진 사람."""
    return {r["requester_agent_id"] for r in c.execute(
        "SELECT requester_agent_id FROM change_requests WHERE session_id = ? "
        "AND kind = 'proceed_without' AND state = 'applied'", (s["session_id"],))}


def _attendees(c, s) -> list[str]:
    """이 회의에 참석하기로 확정한 사람 (확정 제안을 승인한 사람) 중, 도중에 빠지지 않은 사람.

    변경 투표의 유권자이자, 승인 정책이 '동의'로 세는 사람들이다."""
    rows = c.execute("SELECT agent_id FROM approvals WHERE session_id = ? "
                     "AND proposal_version = ? AND verdict = 'approve' ORDER BY agent_id",
                     (s["session_id"], s["proposal_version"])).fetchall()
    gone = _withdrawn(c, s)
    return [r["agent_id"] for r in rows if r["agent_id"] not in gone]


def _invitees(c, s) -> list[str]:
    """캘린더 초대를 받은 사람 중 거절하지 않았고 빠지지도 않은 사람 (참석 확정자 + 미응답자).

    정족수로 확정된 회의에서는 응답하지 않은 선택 참석자도 초대되어 있다. 그들이 '나는 빠질게'를
    요청할 수 있어야 하므로 확정 안내(변경 요청 링크)를 이들에게도 보낸다. 거절한 사람은 제외한다."""
    rejected = {r["agent_id"] for r in c.execute(
        "SELECT agent_id FROM approvals WHERE session_id = ? AND proposal_version = ? "
        "AND verdict = 'reject'", (s["session_id"], s["proposal_version"]))}
    gone = _withdrawn(c, s)
    return [r["agent_id"] for r in c.execute(
        "SELECT agent_id FROM participants WHERE session_id = ? ORDER BY agent_id",
        (s["session_id"],)) if r["agent_id"] not in rejected and r["agent_id"] not in gone]


def _decision_policy(s, attendees: list[str], total: int) -> policy_mod.ApprovalPolicy:
    """변경 투표에 쓰는 정책: 원래 승인 정책과 같은 규칙을, 참석자 집합 위에서 적용한다."""
    pol = policy_mod.ApprovalPolicy.from_row(s)
    if pol.mode == "all":
        return policy_mod.ALL_APPROVE
    needed = min(pol.needed(total), len(attendees))
    return policy_mod.ApprovalPolicy(
        "min_count", min_count=max(1, needed),
        required=tuple(a for a in pol.required if a in attendees))


def _evaluate_votes(c, s, req) -> str:
    attendees = _attendees(c, s)
    total = c.execute("SELECT COUNT(*) n FROM participants WHERE session_id = ?",
                      (s["session_id"],)).fetchone()["n"]
    pol = _decision_policy(s, attendees, total)
    votes = c.execute("SELECT agent_id, verdict FROM change_votes WHERE request_id = ?",
                      (req["request_id"],)).fetchall()
    yes = {v["agent_id"] for v in votes if v["verdict"] == "approve"} | {req["requester_agent_id"]}
    no = {v["agent_id"] for v in votes if v["verdict"] == "reject"}
    # 요청자가 참석 확정자이면 변경에 찬성한 것으로 센다. 확정자가 아니면(유권자가 아니므로) 세지 않는다.
    return policy_mod.evaluate(pol, attendees, yes & set(attendees), no)


def _revoke_change_tokens(c, session_id: str, request_id: str | None, ts: str) -> None:
    if request_id is None:
        c.execute("UPDATE change_tokens SET revoked_at = ? WHERE session_id = ? "
                  "AND used_at IS NULL AND revoked_at IS NULL", (ts, session_id))
    else:
        c.execute("UPDATE change_tokens SET revoked_at = ? WHERE request_id = ? "
                  "AND used_at IS NULL AND revoked_at IS NULL", (ts, request_id))


# ---- 메일 큐 (확정 안내 / 투표 요청 / 결과) ---------------------------------------------------------

def prepare_confirmations(session_id: str) -> int:
    """확정된 회의의 초대 대상(거절하지 않은 사람)마다 '확정 안내(변경 요청 링크 포함)' 메일을
    대기열에 넣는다. 멱등."""
    ts = store.now()
    with store.connect() as c:
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if s is None or s["state"] != "COMMITTED" or s["proposed_slot"] is None:
            return 0
        n = 0
        for aid in _invitees(c, s):
            cur = c.execute(
                "INSERT OR IGNORE INTO change_mail (session_id, request_id, agent_id, kind, "
                "status, attempts, updated_at) VALUES (?, '', ?, 'confirm', 'pending', 0, ?)",
                (session_id, aid, ts))
            n += cur.rowcount
    return n


def _queue_mail(c, session_id: str, request_id: str, agent_ids, kind: str, ts: str) -> None:
    for aid in agent_ids:
        c.execute("INSERT OR IGNORE INTO change_mail (session_id, request_id, agent_id, kind, "
                  "status, attempts, updated_at) VALUES (?,?,?,?,'pending',0,?)",
                  (session_id, request_id, aid, kind, ts))


def _queue_results(c, s, req, ts: str) -> None:
    """요청자와 투표 대상 전원에게 결과 안내를 대기열에 넣는다."""
    ids = set(_attendees(c, s)) | {req["requester_agent_id"]}
    _queue_mail(c, s["session_id"], req["request_id"], sorted(ids), "result", ts)
    c.execute("UPDATE change_mail SET status = 'cancelled', updated_at = ? "
              "WHERE request_id = ? AND kind = 'vote' AND status IN ('pending', 'failed')",
              (ts, req["request_id"]))


def _result_text(req) -> list[str]:
    kind = req["kind"]
    st = req["state"]
    what = KIND_LABEL[kind]
    if st == "applied":
        return {"cancel": ["회의가 취소되었습니다. 캘린더에서 이벤트가 삭제됩니다."],
                "proceed_without": ["참가자 한 분을 제외하고 기존 시간에 진행하기로 했습니다. "
                                    "캘린더 초대가 갱신됩니다."],
                "renegotiate": ["새 시간이 확정되어 기존 캘린더 이벤트의 시간이 바뀌었습니다."]}[kind]
    if st == "denied":
        return [f"'{what}' 요청이 받아들여지지 않았습니다. 기존 일정이 그대로 유지됩니다."]
    if st == "expired":
        return [f"'{what}' 요청의 투표 시간이 지나 종료되었습니다. 기존 일정이 그대로 유지됩니다."]
    return [f"'{what}' 요청을 처리하지 못했습니다. 기존 일정이 그대로 유지됩니다."]


def dispatch_mail(session_id: str, request_id: str = "", kinds=("confirm", "vote", "result"),
                  notifier=None, now: dt.datetime | None = None) -> DispatchSummary:
    """대기 중인(또는 실패한) 변경 관련 메일을 보낸다. 이미 보낸 메일은 다시 보내지 않는다.

    링크가 들어가는 메일(확정 안내, 투표 요청)은 발송 때 새 토큰을 발급하고 같은 사람의
    이전 미사용 토큰은 폐기한다. 조건이 더 이상 유효하지 않으면(회의 시작, 요청 종료) 취소한다.
    """
    summary = DispatchSummary()
    ts = store.now()
    nowdt = _now(now)
    stale = (dt.datetime.now(dt.timezone.utc) - approval_flow.STUCK_SENDING_AFTER).isoformat()
    claimed = []
    with store.connect() as c:
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if s is None:
            return summary
        req = (c.execute("SELECT * FROM change_requests WHERE request_id = ?",
                         (request_id,)).fetchone() if request_id else None)
        start, end = _slot(s) if s["proposed_slot"] is not None else (None, None)
        slot_text = format_slot(start, end) if start else "(미정)"
        rows = c.execute(
            "SELECT m.agent_id, m.kind, m.status, p.display_name, p.email FROM change_mail m "
            "JOIN participants p ON p.session_id = m.session_id AND p.agent_id = m.agent_id "
            "WHERE m.session_id = ? AND m.request_id = ? "
            "AND (m.status IN ('pending', 'failed') OR (m.status = 'sending' AND m.updated_at < ?))"
            " ORDER BY m.kind, m.agent_id", (session_id, request_id, stale)).fetchall()
        for r in rows:
            if r["kind"] not in kinds:
                continue
            kind = r["kind"]
            valid = True
            if kind == "confirm":
                valid = s["state"] == "COMMITTED" and start is not None and start > nowdt
            elif kind == "vote":
                valid = (req is not None and req["state"] == "voting"
                         and req["expires_at"] >= _iso(nowdt))
            if not valid:
                c.execute("UPDATE change_mail SET status = 'cancelled', updated_at = ? "
                          "WHERE session_id = ? AND request_id = ? AND agent_id = ? AND kind = ?",
                          (ts, session_id, request_id, r["agent_id"], kind))
                summary.skipped += 1
                continue
            got = c.execute(
                "UPDATE change_mail SET status = 'sending', attempts = attempts + 1, "
                "updated_at = ? WHERE session_id = ? AND request_id = ? AND agent_id = ? "
                "AND kind = ? AND status = ?",
                (ts, session_id, request_id, r["agent_id"], kind, r["status"]))
            if got.rowcount == 0:
                continue
            url = exp = None
            if kind in ("confirm", "vote"):
                purpose = "request" if kind == "confirm" else "vote"
                exp = (int(start.timestamp()) if kind == "confirm"
                       else int(dt.datetime.fromisoformat(req["expires_at"]).timestamp()))
                c.execute("UPDATE change_tokens SET revoked_at = ? WHERE session_id = ? "
                          "AND request_id = ? AND agent_id = ? AND purpose = ? "
                          "AND used_at IS NULL AND revoked_at IS NULL",
                          (ts, session_id, request_id, r["agent_id"], purpose))
                token, jti = tokens.mint_change(purpose, session_id, request_id,
                                                r["agent_id"], exp)
                c.execute("INSERT INTO change_tokens (jti, purpose, session_id, request_id, "
                          "agent_id, expires_at, created_at) VALUES (?,?,?,?,?,?,?)",
                          (jti, purpose, session_id, request_id, r["agent_id"],
                           _iso(dt.datetime.fromtimestamp(exp, dt.timezone.utc)), ts))
                url = notifier_mod.change_url(purpose, token)
            claimed.append((r["agent_id"], kind, r["display_name"], r["email"], url, exp,
                            slot_text, dict(req) if req is not None else None))

    if not claimed:
        return summary

    config_error = None
    try:
        notifier = notifier or notifier_mod.get_notifier()
    except NotificationError as exc:
        config_error = _safe_error(exc)
    backend = getattr(notifier, "name", "unknown") if notifier else "unconfigured"

    events = []
    for aid, kind, name, email, url, exp, slot_text, reqd in claimed:
        err = config_error
        if err is None:
            try:
                notifier.send(_build_mail(kind, name, email, url, exp, slot_text, reqd,
                                          session_id, aid))
            except Exception as exc:                            # noqa: BLE001
                err = _safe_error(exc)
        _mark_mail(session_id, request_id, aid, kind, "failed" if err else "sent", err, backend)
        if err:
            summary.failed += 1
            summary.errors.append((name, err))
            events.append({"type": "change_mail_failed", "who": name, "detail": err})
        else:
            summary.sent += 1
    events.append({"type": "change_mail", "request": request_id or None, "sent": summary.sent,
                   "failed": summary.failed, "backend": backend})
    _emit(events)
    return summary


DECLINED_TEXT = ["일정 변경 요청을 주최자가 확인했습니다. 이번에는 기존 일정이 그대로 유지됩니다."]


def _build_mail(kind, name, email, url, exp, slot_text, req, session_id, agent_id):
    if kind == "confirm":
        return notifier_mod.render_change_message(
            to_email=email, to_name=name, session_id=session_id, agent_id=agent_id, url=url,
            expires_at=exp, link_label="일정 변경 요청 보내기",
            subject=f"[FairMeet] 회의가 확정되었습니다 — {slot_text}",
            lines=[f"아래 시간으로 회의가 확정되어 캘린더에 등록되었습니다: {slot_text}",
                   "급한 일이 생기면 아래 링크에서 '일정 변경 요청'을 보낼 수 있습니다.",
                   "요청을 보내도 일정은 바뀌지 않으며, 주최자가 확인한 뒤 필요한 경우에만 다시 안내합니다. "
                   "이유를 적을 필요가 없습니다."])
    if kind == "vote":
        return notifier_mod.render_change_message(
            to_email=email, to_name=name, session_id=session_id, agent_id=agent_id, url=url,
            expires_at=exp, link_label="동의 또는 거절하기",
            subject=f"[FairMeet] 일정 변경 요청에 대한 투표 — {slot_text}",
            lines=[f"확정된 회의({slot_text})에 대해 참가자 한 분이 변경을 요청했습니다.",
                   f"요청: {KIND_LABEL[req['kind']]}",
                   "동의 또는 거절을 선택해 주세요. 이유를 적을 필요가 없고, 요청한 사람이 누구인지와 "
                   "사유는 공개되지 않습니다."],
            footer=["투표가 통과되기 전까지 기존 일정은 그대로 유지됩니다.",
                    "다른 참가자의 응답 여부는 표시되지 않습니다."])
    return notifier_mod.render_change_message(
        to_email=email, to_name=name, session_id=session_id, agent_id=agent_id,
        subject=f"[FairMeet] 일정 변경 결과 — {slot_text}",
        lines=[f"회의({slot_text})의 변경 요청 결과입니다."] + (
            _result_text(req) if req is not None else DECLINED_TEXT))


def _mark_mail(session_id, request_id, agent_id, kind, status, error, backend):
    with store.connect() as c:
        c.execute("UPDATE change_mail SET status = ?, last_error = ?, backend = ?, "
                  "updated_at = ? WHERE session_id = ? AND request_id = ? AND agent_id = ? "
                  "AND kind = ? AND status = 'sending'",
                  (status, error, backend, store.now(), session_id, request_id, agent_id, kind))


# ---- 변경 요청 만들기 --------------------------------------------------------------------------------

@dataclass
class RequestView:
    session_id: str
    display_name: str
    slot_text: str
    can_proceed_without: bool
    proceed_without_reason: str = ""
    attends: bool = True           # 참석을 확정한 사람만 재협상·취소를 요청할 수 있다


def _proceed_without_check(s, attendees: list[str], requester: str, total: int) -> str:
    """요청자를 빼고 진행할 수 있는지. 문제가 있으면 사용자에게 보일 문장, 없으면 ''."""
    pol = policy_mod.ApprovalPolicy.from_row(s)
    if requester not in attendees:
        return ""                    # 참석을 확정하지 않은 초대 대상이 빠지는 것은 정족수와 무관하다
    if pol.mode == "all":
        return "전원 참석이 필요한 회의라 한 명이 빠지고 진행할 수 없습니다."
    if requester in pol.required:
        return "필수 참석자는 빠지고 진행할 수 없습니다."
    rest = [a for a in attendees if a != requester]
    if not set(pol.required) <= set(rest) or len(rest) < pol.needed(total):
        return "요청자를 제외하면 승인 기준(정족수)을 채울 수 없어 기존 시간에 진행할 수 없습니다."
    return ""


def inspect_request_token(token: str, now: dt.datetime | None = None) -> RequestView | None:
    """GET 용. 링크가 지금 사용 가능한지와 화면에 보일 이름·시간만 알려 주고 아무것도 바꾸지 않는다.
    참가자 화면은 처리 방법을 묻지 않는다 (처리 방법은 주최자가 고른다)."""
    nowdt = _now(now)
    try:
        claims = tokens.verify_change(token, nowdt.timestamp(), purpose="request")
    except tokens.TokenInvalid:
        return None
    with store.connect() as c:
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?",
                      (claims.session_id,)).fetchone()
        tok = c.execute("SELECT 1 FROM change_tokens WHERE jti = ? AND purpose = 'request' "
                        "AND session_id = ? AND agent_id = ? AND used_at IS NULL "
                        "AND revoked_at IS NULL", (claims.jti, claims.session_id,
                                                   claims.agent_id)).fetchone()
        if s is None or tok is None or s["state"] != "COMMITTED" or s["proposed_slot"] is None:
            return None
        start, end = _slot(s)
        att = _attendees(c, s)
        if start <= nowdt or claims.agent_id not in _invitees(c, s):
            return None
        p = c.execute("SELECT display_name FROM participants WHERE session_id = ? "
                      "AND agent_id = ?", (claims.session_id, claims.agent_id)).fetchone()
        total = c.execute("SELECT COUNT(*) n FROM participants WHERE session_id = ?",
                          (claims.session_id,)).fetchone()["n"]
        why = _proceed_without_check(s, att, claims.agent_id, total)
        return RequestView(claims.session_id, p["display_name"], format_slot(start, end),
                           not why, why, attends=claims.agent_id in att)


def run_created_hook(result) -> None:
    if result.status == "created" and _hooks["on_created"]:
        _hooks["on_created"](result.request_id)


# ---- 투표 -----------------------------------------------------------------------------------------

@dataclass
class VoteView:
    request_id: str
    display_name: str
    kind: str
    slot_text: str
    expires_at: dt.datetime
    rule_text: str


@dataclass
class VoteResult:
    status: str                    # invalid | recorded | accepted | denied
    request_id: str | None = None
    kind: str | None = None


def _rule_text(s, attendees, total) -> str:
    pol = policy_mod.ApprovalPolicy.from_row(s)
    if pol.mode == "all":
        return "이 회의는 참석자 전원이 동의해야 변경됩니다 (요청한 분은 이미 동의한 것으로 봅니다)."
    return ("원래 회의를 확정한 것과 같은 기준이 적용됩니다: 필수 참석자 전원과 총 "
            f"{min(pol.needed(total), len(attendees))}명 이상 동의 (요청한 분은 이미 동의한 것으로 봅니다).")


def inspect_vote_token(token: str, now: dt.datetime | None = None) -> VoteView | None:
    nowdt = _now(now)
    try:
        claims = tokens.verify_change(token, nowdt.timestamp(), purpose="vote")
    except tokens.TokenInvalid:
        return None
    with store.connect() as c:
        req = c.execute("SELECT * FROM change_requests WHERE request_id = ?",
                        (claims.request_id,)).fetchone()
        tok = c.execute("SELECT 1 FROM change_tokens WHERE jti = ? AND purpose = 'vote' "
                        "AND request_id = ? AND agent_id = ? AND used_at IS NULL "
                        "AND revoked_at IS NULL",
                        (claims.jti, claims.request_id, claims.agent_id)).fetchone()
        if (req is None or tok is None or req["state"] != "voting"
                or req["expires_at"] < _iso(nowdt)):
            return None
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?",
                      (req["session_id"],)).fetchone()
        att = _attendees(c, s)
        if claims.agent_id not in att or claims.agent_id == req["requester_agent_id"]:
            return None
        if c.execute("SELECT 1 FROM change_votes WHERE request_id = ? AND agent_id = ?",
                     (claims.request_id, claims.agent_id)).fetchone():
            return None
        p = c.execute("SELECT display_name FROM participants WHERE session_id = ? "
                      "AND agent_id = ?", (s["session_id"], claims.agent_id)).fetchone()
        total = c.execute("SELECT COUNT(*) n FROM participants WHERE session_id = ?",
                          (s["session_id"],)).fetchone()["n"]
        start, end = _slot(s)
        return VoteView(req["request_id"], p["display_name"], req["kind"],
                        format_slot(start, end),
                        dt.datetime.fromisoformat(req["expires_at"]).astimezone(),
                        _rule_text(s, att, total))


def decide_vote(token: str, verdict: str, now: dt.datetime | None = None) -> VoteResult:
    """변경 투표 한 건. 결과는 항상 멱등이다 (무효 링크/이미 사용한 링크는 아무것도 바꾸지 않음)."""
    if verdict not in ("approve", "reject"):
        raise ValueError(f"잘못된 verdict: {verdict}")
    nowdt = _now(now)
    ts = store.now()
    try:
        claims = tokens.verify_change(token, nowdt.timestamp(), purpose="vote")
    except tokens.TokenInvalid:
        return VoteResult("invalid")

    events: list[dict] = []
    result = VoteResult("invalid")
    with store.connect() as c:
        req = c.execute("SELECT * FROM change_requests WHERE request_id = ?",
                        (claims.request_id,)).fetchone()
        if (req is None or req["state"] != "voting" or req["expires_at"] < _iso(nowdt)
                or claims.agent_id == req["requester_agent_id"]):
            return VoteResult("invalid")
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?",
                      (req["session_id"],)).fetchone()
        if s is None or s["state"] != "COMMITTED" or claims.agent_id not in _attendees(c, s):
            return VoteResult("invalid")
        used = c.execute("UPDATE change_tokens SET used_at = ? WHERE jti = ? "
                         "AND purpose = 'vote' AND request_id = ? AND agent_id = ? "
                         "AND used_at IS NULL AND revoked_at IS NULL",
                         (ts, claims.jti, claims.request_id, claims.agent_id))
        if used.rowcount == 0:
            return VoteResult("invalid")
        try:
            c.execute("INSERT INTO change_votes (request_id, agent_id, verdict, decided_at) "
                      "VALUES (?,?,?,?)", (claims.request_id, claims.agent_id, verdict, ts))
        except sqlite3.IntegrityError:
            return VoteResult("invalid")

        outcome = _evaluate_votes(c, s, req)
        if outcome == "approved":
            got = c.execute("UPDATE change_requests SET state = 'applying', updated_at = ? "
                            "WHERE request_id = ? AND state = 'voting'", (ts, req["request_id"]))
            if got.rowcount:
                _revoke_change_tokens(c, s["session_id"], req["request_id"], ts)
                events.append({"type": "change_accepted", "kind": req["kind"],
                               "request": req["request_id"]})
                result = VoteResult("accepted", req["request_id"], req["kind"])
        elif outcome == "failed":
            got = c.execute("UPDATE change_requests SET state = 'denied', updated_at = ? "
                            "WHERE request_id = ? AND state = 'voting'", (ts, req["request_id"]))
            if got.rowcount:
                _revoke_change_tokens(c, s["session_id"], req["request_id"], ts)
                req2 = dict(req, state="denied")
                _queue_results(c, s, req2, ts)
                events.append({"type": "change_denied", "kind": req["kind"],
                               "request": req["request_id"]})
                result = VoteResult("denied", req["request_id"], req["kind"])
        else:
            events.append({"type": "change_vote", "request": req["request_id"]})
            result = VoteResult("recorded", req["request_id"], req["kind"])
    _emit(events)
    return result


def run_vote_hooks(result: VoteResult) -> None:
    if result.status == "accepted" and _hooks["on_accepted"]:
        _hooks["on_accepted"](result.request_id)
    elif result.status == "denied" and _hooks["on_settled"]:
        _hooks["on_settled"](result.request_id)


# ---- 적용 -----------------------------------------------------------------------------------------

def _organizer_id(c, session_id: str) -> str:
    row = c.execute("SELECT agent_id FROM participants WHERE session_id = ? "
                    "AND is_organizer = 1 ORDER BY agent_id LIMIT 1", (session_id,)).fetchone()
    if row is None:
        raise RuntimeError("주최자를 찾을 수 없습니다")
    return row["agent_id"]


def _fail(request_id: str, reason: str) -> None:
    """요청을 실패로 표시한다. 원래 일정은 건드리지 않는다."""
    ts = store.now()
    with store.connect() as c:
        got = c.execute("UPDATE change_requests SET state = 'failed', fail_reason = ?, "
                        "updated_at = ? WHERE request_id = ? AND state IN "
                        "('applying', 'replanning', 'voting')", (reason[:300], ts, request_id))
        if got.rowcount == 0:
            return
        req = c.execute("SELECT * FROM change_requests WHERE request_id = ?",
                        (request_id,)).fetchone()
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?",
                      (req["session_id"],)).fetchone()
        _queue_results(c, s, req, ts)
    _emit([{"type": "change_failed", "request": request_id, "detail": reason[:200]}])


def apply_request(request_id: str, transport, notifier=None,
                  now: dt.datetime | None = None) -> str:
    """투표가 통과한 요청을 적용한다. 이미 처리됐거나 통과하지 않은 요청은 아무것도 하지 않는다.

    같은 요청을 다시 불러도 안전하다: 캘린더 수정/삭제가 멱등이고, 상태 전이는 조건부다.
    """
    with store.connect() as c:
        req = c.execute("SELECT * FROM change_requests WHERE request_id = ?",
                        (request_id,)).fetchone()
        if req is None or req["state"] != "applying":
            return req["state"] if req else "unknown"
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?",
                      (req["session_id"],)).fetchone()
        organizer = _organizer_id(c, s["session_id"])
        event_id = s["google_event_id"]
        parts = c.execute("SELECT agent_id, email, is_organizer FROM participants "
                          "WHERE session_id = ?", (s["session_id"],)).fetchall()
        withdrawn = {r["requester_agent_id"] for r in c.execute(
            "SELECT requester_agent_id FROM change_requests WHERE session_id = ? "
            "AND kind = 'proceed_without' AND state = 'applied'", (s["session_id"],))}
    kind = req["kind"]
    try:
        if s["state"] != "COMMITTED" or not event_id:
            raise RuntimeError("확정된 회의가 아니라 적용할 수 없습니다")
        if kind == "cancel":
            transport.cancel_event(organizer, event_id)          # 이미 없으면 False — 성공으로 본다
            _settle_applied(request_id, s["session_id"], new_session_state="CANCELLED")
        elif kind == "proceed_without":
            remaining = [p["email"] for p in parts
                         if not p["is_organizer"] and p["agent_id"] != req["requester_agent_id"]
                         and p["agent_id"] not in withdrawn]
            transport.update_event(organizer, event_id=event_id, attendee_emails=remaining)
            _settle_applied(request_id, s["session_id"], new_session_state=None)
        else:
            start_replan(request_id, transport, notifier=notifier, now=now)
    except Exception as exc:                                     # noqa: BLE001
        log.warning("변경 적용 실패 %s: %s", request_id, type(exc).__name__)
        _fail(request_id, _safe_error(exc))
    with store.connect() as c:
        return c.execute("SELECT state FROM change_requests WHERE request_id = ?",
                         (request_id,)).fetchone()["state"]


def _settle_applied(request_id: str, session_id: str, new_session_state: str | None) -> None:
    ts = store.now()
    with store.connect() as c:
        got = c.execute("UPDATE change_requests SET state = 'applied', updated_at = ? "
                        "WHERE request_id = ? AND state IN ('applying', 'replanning')",
                        (ts, request_id))
        if got.rowcount == 0:
            return
        if new_session_state:
            cur = c.execute("UPDATE sessions SET state = ?, updated_at = ? "
                            "WHERE session_id = ? AND state = 'COMMITTED'",
                            (new_session_state, ts, session_id))
            if cur.rowcount:
                c.execute("INSERT INTO transitions (session_id, from_state, to_state, detail, "
                          "at) VALUES (?,?,?,?,?)",
                          (session_id, "COMMITTED", new_session_state,
                           f"change request {request_id}", ts))
        req = c.execute("SELECT * FROM change_requests WHERE request_id = ?",
                        (request_id,)).fetchone()
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        _queue_results(c, s, req, ts)
        # 회의가 취소·이동되었으면 그 회의의 모든 변경 링크가 의미를 잃는다. 참석자만 빠진 경우에는
        # 남은 사람들의 변경 요청 링크가 계속 유효해야 하므로 이 요청의 투표 링크만 닫는다.
        _revoke_change_tokens(c, session_id, None if new_session_state else request_id, ts)
        left_agent = req["requester_agent_id"] if req["kind"] == "proceed_without" else None
    if left_agent:
        # 빠진 사람은 이미 받은 회의 자료 링크도 더 이상 열 수 없다 (열람 때의 재확인과 이중으로 막는다)
        try:
            from mediator import materials
            materials.revoke_for_agent(session_id, left_agent)
        except Exception:                                        # noqa: BLE001
            log.exception("자료 열람 권한 회수 실패")
    _emit([{"type": "change_applied", "request": request_id}])


def start_replan(request_id: str, transport, notifier=None,
                 now: dt.datetime | None = None) -> str | None:
    """재협상: 같은 참가자·승인 정책·회의 조건으로 '새 협상 세션'을 열고 승인 요청을 보낸다.

    새 세션이 전원(정책) 승인으로 확정되면 commit.finalize 가 새 이벤트를 만들지 않고 기존
    이벤트의 시간을 고친다. 새 협상이 실패하면 요청이 실패로 끝나고 원래 일정은 그대로다.
    """
    from mediator import drafts
    from mediator.negotiation import run_negotiation
    from mediator.request_model import MeetingRequest

    nowdt = _now(now)
    with store.connect() as c:
        req = c.execute("SELECT * FROM change_requests WHERE request_id = ?",
                        (request_id,)).fetchone()
        if req is None or req["state"] != "applying":
            return None
        parent = c.execute("SELECT * FROM sessions WHERE session_id = ?",
                           (req["session_id"],)).fetchone()
        parts = [dict(r) for r in c.execute(
            "SELECT agent_id, display_name, email, is_organizer FROM participants "
            "WHERE session_id = ? ORDER BY agent_id", (parent["session_id"],))]

    orig_start = dt.datetime.fromisoformat(req["original_start"])
    orig_end = dt.datetime.fromisoformat(req["original_end"])
    tomorrow = (nowdt.astimezone(KST) + dt.timedelta(days=1)).date()
    days = config.CHANGE_SEARCH_DAYS
    slots = build_slots(tomorrow, parent["mode"], parent["duration_min"], days=days)

    pol = policy_mod.ApprovalPolicy.from_row(parent)
    effects: dict = {"excluded": [], "penalty": {}, "agent_options": {}, "manual_checks": []}
    constraints = None
    if parent["constraints_json"]:
        mreq = MeetingRequest.from_dict(json.loads(parent["constraints_json"]))
        mreq.date_start, mreq.date_end = tomorrow, tomorrow + dt.timedelta(days=days - 1)
        mreq.duration_min = parent["duration_min"]
        mreq.issues = [i for i in mreq.issues if i.origin == "parse"]
        denv = drafts.DraftEnv(
            participants=[{"agent_id": p["agent_id"], "name": p["display_name"],
                           "is_organizer": bool(p["is_organizer"])} for p in parts],
            capabilities={p["agent_id"]: set(transport.capabilities(p["agent_id"]) or ())
                          for p in parts},
            now=nowdt)
        ev, _ = drafts.refresh(mreq, denv)
        effects = ev.slot_effects()
        constraints = mreq.to_dict()
    # 원래 회의 시간과 겹치는 슬롯은 다시 제안하지 않는다
    same = {i for i, (s, e) in enumerate(slots) if s < orig_end and orig_start < e}
    effects["excluded"] = sorted(set(effects.get("excluded", [])) | same)

    sid = new_session_id()
    store.create_session(sid, parent["group_id"], parent["mode"], parent["duration_min"],
                         slots, parts, policy=pol, constraints=constraints,
                         slot_effects=effects, parent_session_id=parent["session_id"],
                         change_request_id=request_id)
    ts = store.now()
    with store.connect() as c:
        got = c.execute("UPDATE change_requests SET state = 'replanning', followup_session_id = ?, "
                        "updated_at = ? WHERE request_id = ? AND state = 'applying'",
                        (sid, ts, request_id))
        if got.rowcount == 0:
            return None

    lam = config.LAMBDA_WORK if parent["mode"] == "work" else config.LAMBDA_SOCIAL
    outcome = run_negotiation(sid, transport, slots, parent["group_id"], lam=lam,
                              emit=_hooks["emit"])
    if outcome.state == "PENDING_HUMAN_APPROVAL":
        approval_flow.dispatch_notifications(sid, notifier)
        _emit([{"type": "change_replanning", "request": request_id, "session": sid}])
    else:
        _fail(request_id, f"재협상에서 새 시간을 찾지 못했습니다 ({outcome.state})")
    return sid


# ---- 조율: 재협상 결과 반영 / 만료 / 멈춘 적용 -------------------------------------------------------

def reconcile_request(request_id: str) -> str | None:
    """재협상 중인 요청을 새 세션의 결과에 맞춰 마무리한다. 멱등."""
    with store.connect() as c:
        req = c.execute("SELECT * FROM change_requests WHERE request_id = ?",
                        (request_id,)).fetchone()
        if req is None:
            return None
        if req["state"] != "replanning" or not req["followup_session_id"]:
            return req["state"]
        f = c.execute("SELECT state FROM sessions WHERE session_id = ?",
                      (req["followup_session_id"],)).fetchone()
    if f is None:
        return req["state"]
    if f["state"] == "COMMITTED":
        _settle_applied(request_id, req["session_id"], new_session_state="RESCHEDULED")
    elif f["state"] in store.TERMINAL_STATES:
        _fail(request_id, f"재협상이 확정되지 않았습니다 ({f['state']})")
    with store.connect() as c:
        return c.execute("SELECT state FROM change_requests WHERE request_id = ?",
                         (request_id,)).fetchone()["state"]


def reconcile_all() -> list[str]:
    """재협상 중인 모든 요청을 새 세션의 현재 결과에 맞춰 마무리한다. 멱등."""
    with store.connect() as c:
        ids = [r["request_id"] for r in c.execute(
            "SELECT request_id FROM change_requests WHERE state = 'replanning'")]
    settled = []
    for rid in ids:
        if reconcile_request(rid) in ("applied", "failed"):
            settled.append(rid)
            if _hooks["on_settled"]:
                _hooks["on_settled"](rid)
    return settled


def reconcile_for_session(session_id: str) -> None:
    """세션(재협상으로 만들어진 것일 수 있음)이 끝났을 때 부른다. 관련 없는 세션이면 아무 일도 없다."""
    row = store.get_session(session_id)
    if row is None or not row["change_request_id"]:
        return
    state = reconcile_request(row["change_request_id"])
    if state in ("applied", "failed") and _hooks["on_settled"]:
        _hooks["on_settled"](row["change_request_id"])


def expire_stale_changes(now: dt.datetime | None = None) -> list[str]:
    """투표 시간이 지난 변경 요청을 만료시킨다. 기존 일정은 그대로다."""
    nowdt = _now(now)
    ts = store.now()
    done = []
    with store.connect() as c:
        rows = c.execute("SELECT * FROM change_requests WHERE state = 'voting' "
                         "AND expires_at < ?", (_iso(nowdt),)).fetchall()
        for req in rows:
            got = c.execute("UPDATE change_requests SET state = 'expired', updated_at = ? "
                            "WHERE request_id = ? AND state = 'voting'", (ts, req["request_id"]))
            if got.rowcount:
                s = c.execute("SELECT * FROM sessions WHERE session_id = ?",
                              (req["session_id"],)).fetchone()
                _revoke_change_tokens(c, req["session_id"], req["request_id"], ts)
                _queue_results(c, s, dict(req, state="expired"), ts)
                done.append(req["request_id"])
    if done:
        _emit([{"type": "change_expired", "request": r} for r in done])
    expire_stale_intakes(now)
    return done


def resume_stuck_applications(transport, notifier=None, now: dt.datetime | None = None
                              ) -> list[str]:
    """서버가 적용 도중 멈춰 'applying' 으로 남은 요청을 다시 적용한다 (적용은 멱등이다)."""
    cutoff = (dt.datetime.now(dt.timezone.utc) - STUCK_APPLYING_AFTER).isoformat()
    with store.connect() as c:
        ids = [r["request_id"] for r in c.execute(
            "SELECT request_id FROM change_requests WHERE state = 'applying' "
            "AND updated_at < ?", (cutoff,))]
    for rid in ids:
        apply_request(rid, transport, notifier=notifier, now=now)
    return ids


# ---- 화면용 뷰 (요청자의 신원·사유·링크·토큰은 없다) --------------------------------------------------

def change_view(session_id: str) -> dict | None:
    """관리자 대시보드용 변경 요청 요약. 확정되지 않은 세션이면 None."""
    with store.connect() as c:
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if s is None:
            return None
        reqs = c.execute("SELECT * FROM change_requests WHERE session_id = ? "
                         "ORDER BY created_at DESC", (session_id,)).fetchall()
        mail = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM change_mail WHERE session_id = ? "
            "AND request_id = '' AND kind = 'confirm' GROUP BY status", (session_id,))}
        att = _attendees(c, s) if s["state"] in ("COMMITTED", "RESCHEDULED", "CANCELLED") else []
        items = []
        for r in reqs:
            votes = c.execute("SELECT COUNT(*) n FROM change_votes WHERE request_id = ?",
                              (r["request_id"],)).fetchone()["n"]
            items.append({
                "requestId": r["request_id"], "kind": r["kind"], "kindLabel": KIND_LABEL[r["kind"]],
                "state": r["state"], "stateLabel": STATE_LABEL[r["state"]],
                "votes": {"responded": votes, "total": len(
                    [a for a in att if a != r["requester_agent_id"]])},
                "followupSessionId": r["followup_session_id"], "reason": r["fail_reason"],
                "expiresAt": r["expires_at"], "createdAt": r["created_at"]})
        parent = s["parent_session_id"] if s["change_request_id"] else None
        intakes = [{"intakeId": r["intake_id"], "state": r["state"], "createdAt": r["created_at"],
                    "decision": r["decision"] or ""} for r in c.execute(
                        "SELECT * FROM change_intakes WHERE session_id = ? ORDER BY created_at DESC",
                        (session_id,))]
    if not items and not mail and not parent and not intakes:
        return None
    return {"confirmationMails": mail, "requests": items, "intakes": intakes,
            "openIntakes": len([i for i in intakes if i["state"] == "open"]),
            "open": next((i for i in items if i["state"] in OPEN_STATES), None),
            "replaces": parent}


# ---- 변경 요청 접수와 주최자의 결정 (v5) ---------------------------------------------------------------

INTAKE_DECISIONS = KINDS + ("decline",)
DECISION_LABEL = {"renegotiate": "전체 참가자의 새 시간 재협상", "proceed_without": "요청자를 제외하고 기존 시간에 진행",
                  "cancel": "회의 취소 제안", "decline": "요청 거절 · 기존 일정 유지"}


@dataclass
class IntakeResult:
    status: str                    # created | duplicate | invalid | rejected
    intake_id: str | None = None
    message: str = ""


@dataclass
class DecideResult:
    status: str                    # created(투표가 열림) | declined | rejected | invalid
    request_id: str | None = None
    message: str = ""
    kind: str | None = None


def _log(c, session_id: str, event: str, actor: str, *, intake_id: str = "", request_id: str = "",
         detail: str = "", ts: str | None = None) -> None:
    """처리 이력. 사유·본문은 남기지 않는다."""
    c.execute("INSERT INTO change_events (session_id, intake_id, request_id, at, event, actor, detail) "
              "VALUES (?,?,?,?,?,?,?)",
              (session_id, intake_id, request_id, ts or store.now(), event, actor, detail[:200]))


def submit_intake(token: str, now: dt.datetime | None = None) -> IntakeResult:
    """참가자가 변경 링크로 '일정 변경 요청'을 보낸다. 일정을 바꾸지 않고 다른 참가자에게 아무것도 보내지 않는다.

    같은 사람이 이미 열어 둔 요청이 있으면 새로 만들지 않고 duplicate 로 답한다 (중복 제출은 한 번만 처리된다).
    링크는 요청이 실제로 접수될 때만 소비된다.
    """
    nowdt = _now(now)
    ts = store.now()
    try:
        claims = tokens.verify_change(token, nowdt.timestamp(), purpose="request")
    except tokens.TokenInvalid:
        return IntakeResult("invalid")

    iid = "i" + secrets.token_hex(8)
    try:
        with store.connect() as c:
            s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (claims.session_id,)).fetchone()
            tok = c.execute("SELECT 1 FROM change_tokens WHERE jti = ? AND purpose = 'request' "
                            "AND session_id = ? AND agent_id = ? AND used_at IS NULL "
                            "AND revoked_at IS NULL", (claims.jti, claims.session_id, claims.agent_id)).fetchone()
            if s is None or tok is None:
                return IntakeResult("invalid")
            if s["state"] != "COMMITTED" or s["proposed_slot"] is None or not s["google_event_id"]:
                return IntakeResult("rejected", message="확정된 회의가 아니라 변경을 요청할 수 없습니다")
            start, _ = _slot(s)
            if start <= nowdt:
                return IntakeResult("rejected", message="이미 시작했거나 지난 회의는 변경을 요청할 수 없습니다")
            if claims.agent_id not in _invitees(c, s):
                return IntakeResult("invalid")
            dup = c.execute("SELECT intake_id FROM change_intakes WHERE session_id = ? "
                            "AND requester_agent_id = ? AND state = 'open'",
                            (claims.session_id, claims.agent_id)).fetchone()
            if dup:
                return IntakeResult("duplicate", dup["intake_id"])
            try:
                c.execute("INSERT INTO change_intakes (intake_id, session_id, requester_agent_id, state, "
                          "created_at, updated_at) VALUES (?,?,?,'open',?,?)",
                          (iid, claims.session_id, claims.agent_id, ts, ts))
            except sqlite3.IntegrityError:                      # 동시에 들어온 같은 사람의 요청이 먼저 만들어졌다
                return IntakeResult("duplicate")
            used = c.execute("UPDATE change_tokens SET used_at = ? WHERE jti = ? "
                             "AND used_at IS NULL AND revoked_at IS NULL", (ts, claims.jti))
            if used.rowcount == 0:
                raise sqlite3.IntegrityError("token already used")
            _log(c, claims.session_id, "submitted", "participant", intake_id=iid, ts=ts)
            c.execute("INSERT INTO transitions (session_id, from_state, to_state, detail, at) "
                      "VALUES (?,?,?,?,?)", (claims.session_id, "COMMITTED", "COMMITTED",
                                             "change intake submitted", ts))
    except sqlite3.IntegrityError:
        return IntakeResult("invalid")
    _emit([{"type": "change_intake", "intake": iid}])            # 화면에는 '확인이 필요한 요청 1건'만 알린다
    return IntakeResult("created", iid, "변경 요청이 접수되었습니다")


def _intake_options(c, s, it) -> list[dict]:
    """주최자가 고를 수 있는 처리와, 못 고르는 이유. 정책은 원래 회의의 승인 정책 그대로다."""
    attendees = _attendees(c, s)
    total = c.execute("SELECT COUNT(*) n FROM participants WHERE session_id = ?",
                      (s["session_id"],)).fetchone()["n"]
    who = it["requester_agent_id"]
    attends = who in attendees
    open_req = c.execute("SELECT 1 FROM change_requests WHERE session_id = ? AND state IN "
                         "('voting', 'applying', 'replanning')", (s["session_id"],)).fetchone()
    busy = "이미 진행 중인 다른 변경 요청이 끝난 뒤에 고를 수 있어요." if open_req else ""
    no_attend = "참석을 확정하지 않은 분의 요청이라 고를 수 없어요."
    why_without = _proceed_without_check(s, attendees, who, total)
    opts = [
        {"action": "renegotiate", "label": DECISION_LABEL["renegotiate"],
         "hint": "참가자에게 투표를 받은 뒤 새 시간을 찾고, 다시 승인받아 기존 캘린더 이벤트를 수정해요.",
         "reason": busy or ("" if attends else no_attend)},
        {"action": "proceed_without", "label": DECISION_LABEL["proceed_without"],
         "hint": "다른 참가자에게 동의를 받은 뒤 요청자만 초대에서 빠져요.",
         "reason": busy or why_without},
        {"action": "cancel", "label": DECISION_LABEL["cancel"],
         "hint": "참가자에게 투표를 받아 통과하면 회의를 취소해요.",
         "reason": busy or ("" if attends else no_attend)},
        {"action": "decline", "label": DECISION_LABEL["decline"],
         "hint": "다른 참가자에게 아무것도 보내지 않고 기존 일정을 그대로 두어요. 요청한 분께만 알려요.",
         "reason": ""},
    ]
    for o in opts:
        o["enabled"] = not o["reason"]
    return opts


def decide_intake(intake_id: str, action: str, *, actor: str = "organizer",
                  now: dt.datetime | None = None) -> DecideResult:
    """주최자가 접수된 변경 요청의 처리 방법을 고른다. 이때 비로소 투표가 열리고 투표 메일이 나간다."""
    if action not in INTAKE_DECISIONS:
        return DecideResult("rejected", message="알 수 없는 처리 방법입니다")
    nowdt = _now(now)
    ts = store.now()
    rid = "r" + secrets.token_hex(8)
    try:
        with store.connect() as c:
            it = c.execute("SELECT * FROM change_intakes WHERE intake_id = ?", (intake_id,)).fetchone()
            if it is None or it["state"] != "open":
                return DecideResult("invalid", message="이미 처리되었거나 없는 요청이에요")
            s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (it["session_id"],)).fetchone()
            who = it["requester_agent_id"]

            if action == "decline":
                got = c.execute("UPDATE change_intakes SET state = 'declined', decision = 'decline', "
                                "updated_at = ?, decided_at = ?, decided_by = ? "
                                "WHERE intake_id = ? AND state = 'open'", (ts, ts, actor, intake_id))
                if got.rowcount == 0:
                    return DecideResult("invalid", message="이미 처리되었거나 없는 요청이에요")
                _log(c, s["session_id"], "decided", actor, intake_id=intake_id, detail="decline", ts=ts)
                _queue_mail(c, s["session_id"], intake_id, [who], "result", ts)
                _emit([{"type": "change_declined", "intake": intake_id}])
                return DecideResult("declined", message="요청을 거절하고 기존 일정을 유지해요")

            if s["state"] != "COMMITTED" or s["proposed_slot"] is None or not s["google_event_id"]:
                return DecideResult("rejected", message="확정된 회의가 아니라 처리할 수 없어요")
            start, end = _slot(s)
            if start <= nowdt:
                return DecideResult("rejected", message="이미 시작했거나 지난 회의는 바꿀 수 없어요")
            attendees = _attendees(c, s)
            if who not in _invitees(c, s):
                return DecideResult("rejected", message="요청한 분이 더 이상 이 회의의 초대 대상이 아니에요")
            option = next(o for o in _intake_options(c, s, it) if o["action"] == action)
            if not option["enabled"]:
                return DecideResult("rejected", message=option["reason"])
            expires = min(nowdt + dt.timedelta(minutes=config.CHANGE_TTL_MIN), start)
            try:
                c.execute("INSERT INTO change_requests (request_id, session_id, kind, requester_agent_id, "
                          "state, original_start, original_end, created_at, updated_at, expires_at) "
                          "VALUES (?,?,?,?,'voting',?,?,?,?,?)",
                          (rid, s["session_id"], action, who, start.isoformat(), end.isoformat(),
                           ts, ts, _iso(expires)))
            except sqlite3.IntegrityError:                     # 동시에 다른 요청이 열렸다
                return DecideResult("rejected",
                                    message="이미 진행 중인 다른 변경 요청이 끝난 뒤에 고를 수 있어요.")
            got = c.execute("UPDATE change_intakes SET state = 'handled', decision = ?, request_id = ?, "
                            "updated_at = ?, decided_at = ?, decided_by = ? "
                            "WHERE intake_id = ? AND state = 'open'", (action, rid, ts, ts, actor, intake_id))
            if got.rowcount == 0:
                raise sqlite3.IntegrityError("intake already decided")
            _queue_mail(c, s["session_id"], rid, [a for a in attendees if a != who], "vote", ts)
            _log(c, s["session_id"], "decided", actor, intake_id=intake_id, request_id=rid, detail=action, ts=ts)
            _log(c, s["session_id"], "vote_mail_queued", "system", intake_id=intake_id, request_id=rid,
                 detail=str(len([a for a in attendees if a != who])), ts=ts)
            c.execute("INSERT INTO transitions (session_id, from_state, to_state, detail, at) "
                      "VALUES (?,?,?,?,?)", (s["session_id"], "COMMITTED", "COMMITTED",
                                             f"change request {action} opened", ts))
    except sqlite3.IntegrityError:
        return DecideResult("invalid", message="이미 처리되었거나 없는 요청이에요")
    _emit([{"type": "change_requested", "kind": action, "request": rid}])
    return DecideResult("created", rid, "다른 참가자에게 투표를 요청했어요", action)


def intake_cards(session_id: str | None = None, *, only_open: bool = True) -> list[dict]:
    """관리자 화면용 변경 요청 카드. 요청자 이름은 처리에 필요한 관리자에게만 보이고 사유는 없다."""
    with store.connect() as c:
        q = "SELECT * FROM change_intakes"
        args: list = []
        conds = []
        if session_id:
            conds.append("session_id = ?")
            args.append(session_id)
        if only_open:
            conds.append("state = 'open'")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        rows = c.execute(q + " ORDER BY created_at DESC LIMIT 50", args).fetchall()
        out = []
        for it in rows:
            s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (it["session_id"],)).fetchone()
            p = c.execute("SELECT display_name FROM participants WHERE session_id = ? AND agent_id = ?",
                          (it["session_id"], it["requester_agent_id"])).fetchone()
            slot = format_slot(*_slot(s)) if s and s["proposed_slot"] is not None else ""
            out.append({
                "intakeId": it["intake_id"], "meetingId": it["session_id"], "state": it["state"],
                "requester": p["display_name"] if p else "참가자", "slot": slot,
                "createdAt": it["created_at"], "decision": it["decision"],
                "decisionLabel": DECISION_LABEL.get(it["decision"] or "", ""),
                "options": _intake_options(c, s, it) if (s and it["state"] == "open") else [],
            })
        return out


def expire_stale_intakes(now: dt.datetime | None = None) -> list[str]:
    """회의가 이미 시작했거나 더는 확정 상태가 아니면 열려 있던 변경 요청을 닫는다."""
    nowdt = _now(now)
    ts = store.now()
    done = []
    with store.connect() as c:
        for it in c.execute("SELECT * FROM change_intakes WHERE state = 'open'").fetchall():
            s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (it["session_id"],)).fetchone()
            stale = (s is None or s["state"] != "COMMITTED" or s["proposed_slot"] is None
                     or _slot(s)[0] <= nowdt)
            if stale:
                got = c.execute("UPDATE change_intakes SET state = 'expired', updated_at = ? "
                                "WHERE intake_id = ? AND state = 'open'", (ts, it["intake_id"]))
                if got.rowcount:
                    _log(c, it["session_id"], "expired", "system", intake_id=it["intake_id"], ts=ts)
                    done.append(it["intake_id"])
    return done


def intake_events(session_id: str, limit: int = 100) -> list[dict]:
    """감사용 처리 이력 (사유 없음, 요청자 이름 없음)."""
    with store.connect() as c:
        return [dict(r) for r in c.execute(
            "SELECT at, event, actor, detail, intake_id, request_id FROM change_events "
            "WHERE session_id = ? ORDER BY id LIMIT ?", (session_id, limit))]
