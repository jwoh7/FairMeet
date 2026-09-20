"""회의 자료 분석 결과의 보관·수신자 결정·전달·열람 권한.

원칙
  - PDF 원본은 저장하지 않는다. 분석 결과만 보관 기간(FAIRMEET_MATERIAL_RETENTION_DAYS) 동안 둔다.
  - 자동으로 아무에게도 보내지 않는다. 주최자(관리자)가 수신자 수를 확인하고 승인해야 발송된다.
  - 수신 대상은 '이 회의에 참석하기로 확정한 사람'뿐이다 (확정된 제안을 승인한 사람, 도중에 빠지지 않은 사람).
    정족수 방식에서 거절했거나 응답하지 않은 사람, 일정 변경으로 빠진 사람은 받지 못한다.
  - 열람 링크는 서명·만료가 있고 (회의, 자료, 수신자) 에 묶여 있다. 링크를 열 때마다 서버가 지금도 그 사람이
    참석자인지 다시 확인하므로, 나중에 빠진 사람은 링크를 알아도 볼 수 없다.
  - PDF 원본은 이메일에 첨부하지 않는다. 메일에는 짧은 요약과 개인 링크만 들어간다.
  - 관리자 화면에는 사람별 발송 성공/실패만, 참가자에게는 다른 사람의 수신 여부를 보여 주지 않는다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from dataclasses import dataclass, field

from common import config
from common.slots import format_slot
from mediator import change_flow, store, tokens
from mediator import notifier as notifier_mod
from mediator.notifier import NotificationError

log = logging.getLogger("fairmeet.materials")

STATUS_LABEL = {"pending": "발송 대기", "sending": "발송 중", "sent": "발송 완료",
                "failed": "발송 실패", "revoked": "권한 회수됨"}


class MaterialError(Exception):
    """사용자에게 그대로 보일 수 있는 오류."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).isoformat()


# ---- 보관 -------------------------------------------------------------------------------------

def save_result(session_id: str, result: dict, mode: str) -> str:
    if mode not in ("llm", "rules"):
        raise ValueError("mode")
    prep_id = "p" + secrets.token_hex(10)
    now = dt.datetime.now(dt.timezone.utc)
    exp = now + dt.timedelta(days=config.MATERIAL_RETENTION_DAYS)
    result = dict(result)
    result["meta"] = dict(result.get("meta") or {}, mode=mode)
    with store.connect() as c:
        if c.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone() is None:
            raise MaterialError(404, "회의를 찾을 수 없어요.")
        c.execute("INSERT INTO prep_results (prep_id, session_id, mode, result_json, created_at, "
                  "expires_at) VALUES (?,?,?,?,?,?)",
                  (prep_id, session_id, mode, json.dumps(result, ensure_ascii=False),
                   _iso(now), _iso(exp)))
    return prep_id


def purge_expired() -> int:
    with store.connect() as c:
        return c.execute("DELETE FROM prep_results WHERE expires_at < ?", (store.now(),)).rowcount


def _prep(c, prep_id: str):
    row = c.execute("SELECT * FROM prep_results WHERE prep_id = ?", (prep_id,)).fetchone()
    if row is None or row["revoked_at"] or row["expires_at"] < store.now():
        return None
    return row


def get_result(prep_id: str) -> dict | None:
    with store.connect() as c:
        row = _prep(c, prep_id)
    return None if row is None else {"prepId": prep_id, "sessionId": row["session_id"],
                                     "mode": row["mode"], "createdAt": row["created_at"],
                                     "result": json.loads(row["result_json"])}


def list_for_session(session_id: str) -> list[dict]:
    with store.connect() as c:
        rows = c.execute("SELECT prep_id FROM prep_results WHERE session_id = ? AND revoked_at IS NULL "
                         "AND expires_at >= ? ORDER BY created_at DESC", (session_id, store.now())
                         ).fetchall()
    return [shares_view(r["prep_id"]) for r in rows]


# ---- 수신자 -----------------------------------------------------------------------------------

def _effective_session(c, s):
    """일정이 재협상으로 옮겨졌으면 이어받은 세션이 지금의 회의다."""
    if s["state"] == "RESCHEDULED":
        from mediator import present
        return present._followup_of(c, s["session_id"]) or s
    return s


@dataclass
class Recipients:
    ok: bool
    reason: str = ""
    people: list[dict] = field(default_factory=list)        # [{agent_id, name, email}]  (email 은 서버 안에서만)

    @property
    def count(self) -> int:
        return len(self.people)


def recipients_for(session_id: str) -> Recipients:
    with store.connect() as c:
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if s is None:
            return Recipients(False, "회의를 찾을 수 없어요.")
        eff = _effective_session(c, s)
        if eff["state"] != "COMMITTED":
            return Recipients(False, "회의가 확정된 뒤에 참석자에게 보낼 수 있어요.")
        att = set(change_flow._attendees(c, eff))
        people = [{"agent_id": p["agent_id"], "name": p["display_name"], "email": p["email"]}
                  for p in c.execute("SELECT * FROM participants WHERE session_id = ? "
                                     "ORDER BY agent_id", (eff["session_id"],))
                  if p["agent_id"] in att]
    if not people:
        return Recipients(False, "참석이 확정된 참가자가 없어요.")
    return Recipients(True, "", people)


def is_attendee(session_id: str, agent_id: str) -> bool:
    """지금 이 회의에 참석이 확정된 사람인가 (열람 때마다 다시 확인한다)."""
    r = recipients_for(session_id)
    return r.ok and any(p["agent_id"] == agent_id for p in r.people)


# ---- 미리보기 · 발송 ----------------------------------------------------------------------------

def shares_view(prep_id: str) -> dict:
    with store.connect() as c:
        row = c.execute("SELECT * FROM prep_results WHERE prep_id = ?", (prep_id,)).fetchone()
        shares = {r["agent_id"]: r for r in c.execute(
            "SELECT * FROM material_shares WHERE prep_id = ?", (prep_id,))}
        names = {p["agent_id"]: p["display_name"] for p in c.execute(
            "SELECT DISTINCT agent_id, display_name FROM participants WHERE session_id = ?",
            (row["session_id"],))} if row else {}
    rec = recipients_for(row["session_id"]) if row else Recipients(False)
    people = []
    for p in rec.people:
        sh = shares.get(p["agent_id"])
        st = sh["status"] if sh else None
        people.append({"name": p["name"], "status": st or "none",
                       "label": STATUS_LABEL.get(st, "아직 보내지 않음")})
    # 발송 뒤에 권한이 회수된 사람도 관리자에게는 보인다 (이름과 상태만)
    for aid, sh in shares.items():
        if sh["status"] == "revoked":
            people.append({"name": names.get(aid, "참가자"), "status": "revoked",
                           "label": STATUS_LABEL["revoked"]})
    counts = {k: sum(1 for p in people if p["status"] == k) for k in STATUS_LABEL}
    res = json.loads(row["result_json"]) if row else {}
    return {"prepId": prep_id, "mode": row["mode"] if row else None,
            "createdAt": row["created_at"] if row else None,
            "canSend": rec.ok, "cannotSendReason": rec.reason, "recipientCount": rec.count,
            "recipients": people, "counts": counts,
            "summary": (res.get("summary") or "")[:300]}


def send(prep_id: str, expected_count: int, notifier=None) -> dict:
    """주최자가 수신자 수를 확인하고 승인한 뒤 발송한다. 이미 보낸 사람에게는 다시 보내지 않는다."""
    with store.connect() as c:
        prep = _prep(c, prep_id)
    if prep is None:
        raise MaterialError(404, "분석 결과를 찾을 수 없거나 보관 기간이 지났어요.")
    rec = recipients_for(prep["session_id"])
    if not rec.ok:
        raise MaterialError(409, rec.reason)
    if not isinstance(expected_count, int) or isinstance(expected_count, bool) \
            or expected_count != rec.count:
        raise MaterialError(409, "수신자가 바뀌었어요. 수신자 수를 다시 확인해 주세요.")

    ts = store.now()
    allowed = {p["agent_id"] for p in rec.people}
    claimed = []
    with store.connect() as c:
        for p in rec.people:
            c.execute("INSERT OR IGNORE INTO material_shares (prep_id, agent_id, status, updated_at) "
                      "VALUES (?,?, 'pending', ?)", (prep_id, p["agent_id"], ts))
        # 이제 수신 자격이 없는 사람은 권한을 회수한다
        for r in c.execute("SELECT agent_id FROM material_shares WHERE prep_id = ? "
                           "AND status != 'revoked'", (prep_id,)).fetchall():
            if r["agent_id"] not in allowed:
                _revoke_tx(c, prep_id, r["agent_id"], ts)
        stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)).isoformat()
        for p in rec.people:
            got = c.execute(
                "UPDATE material_shares SET status = 'sending', attempts = attempts + 1, "
                "updated_at = ? WHERE prep_id = ? AND agent_id = ? AND (status IN ('pending', 'failed') "
                "OR (status = 'sending' AND updated_at < ?))", (ts, prep_id, p["agent_id"], stale))
            if got.rowcount == 0:
                continue
            exp = min(int(dt.datetime.now(dt.timezone.utc).timestamp()) + config.MATERIAL_LINK_TTL_H * 3600,
                      int(dt.datetime.fromisoformat(prep["expires_at"]).timestamp()))
            c.execute("UPDATE material_tokens SET revoked_at = ? WHERE prep_id = ? AND agent_id = ? "
                      "AND revoked_at IS NULL", (ts, prep_id, p["agent_id"]))
            token, jti = tokens.mint_material(prep_id, prep["session_id"], p["agent_id"], exp)
            c.execute("INSERT INTO material_tokens (jti, prep_id, agent_id, expires_at, created_at) "
                      "VALUES (?,?,?,?,?)",
                      (jti, prep_id, p["agent_id"],
                       _iso(dt.datetime.fromtimestamp(exp, dt.timezone.utc)), ts))
            claimed.append((p, token, exp))
        sess = c.execute("SELECT * FROM sessions WHERE session_id = ?", (prep["session_id"],)).fetchone()
        eff = _effective_session(c, sess)
        slot_text = "(미정)"
        if eff["proposed_slot"] is not None:
            a, b = json.loads(eff["slots_json"])[eff["proposed_slot"]]
            slot_text = format_slot(dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b))

    summary_text = (json.loads(prep["result_json"]).get("summary") or "")[:300]
    error_cfg = None
    try:
        notifier = notifier or notifier_mod.get_notifier()
    except NotificationError as exc:
        error_cfg = notifier_mod.redact(str(exc))
    sent = failed = 0
    for p, token, exp in claimed:
        err = error_cfg
        if err is None:
            try:
                notifier.send(_build_mail(p, token, exp, slot_text, summary_text, prep["mode"],
                                          prep["session_id"]))
            except Exception as exc:                                    # noqa: BLE001
                err = notifier_mod.redact(str(exc) or type(exc).__name__)[:200]
        with store.connect() as c:
            c.execute("UPDATE material_shares SET status = ?, last_error = ?, updated_at = ? "
                      "WHERE prep_id = ? AND agent_id = ? AND status = 'sending'",
                      ("failed" if err else "sent", err, store.now(), prep_id, p["agent_id"]))
        sent += 0 if err else 1
        failed += 1 if err else 0
    log.info("자료 발송 %s: 성공 %d 실패 %d", prep_id[:6], sent, failed)
    return {"sent": sent, "failed": failed, "skipped": rec.count - len(claimed)}


def _build_mail(p: dict, token: str, exp: int, slot_text: str, summary: str, mode: str,
                session_id: str):
    label = ("규칙 기반의 제한적인 정리예요 (AI 다중 에이전트 분석이 아니에요)."
             if mode == "rules" else "AI가 정리한 결과예요. 근거를 확인하지 못한 주장은 표시돼 있어요.")
    lines = [f"{slot_text} 회의의 자료 정리를 공유해 드려요. 이 회의에 참석하기로 확정된 분께만 보내는 메일이에요.",
             f"요약: {summary}" if summary else "요약을 만들지 못했어요. 링크에서 자세한 내용을 확인해 주세요.",
             label, "원본 PDF는 첨부하지 않았어요. 아래 링크에서 정리된 내용을 볼 수 있어요."]
    url = f"{config.PUBLIC_BASE_URL.rstrip('/')}/materials/{token}"
    return notifier_mod.render_change_message(
        to_email=p["email"], to_name=p["name"], session_id=session_id, agent_id=p["agent_id"],
        subject=f"[FairMeet] 회의 자료 정리 — {slot_text}", lines=lines, url=url,
        link_label="회의 자료 열기", expires_at=exp,
        footer=["이 링크는 회의 참석자에게만 열려요. 다른 사람에게 전달하지 마세요.",
                "참석 상태가 바뀌면 링크로 더 이상 열 수 없어요."])


def _revoke_tx(c, prep_id: str, agent_id: str, ts: str) -> None:
    c.execute("UPDATE material_shares SET status = 'revoked', updated_at = ? "
              "WHERE prep_id = ? AND agent_id = ?", (ts, prep_id, agent_id))
    c.execute("UPDATE material_tokens SET revoked_at = ? WHERE prep_id = ? AND agent_id = ? "
              "AND revoked_at IS NULL", (ts, prep_id, agent_id))


def revoke_for_agent(session_id: str, agent_id: str) -> int:
    """이 회의의 모든 자료에서 한 사람의 열람 권한을 회수한다 (열람 때의 재확인과 별개로 기록을 남긴다)."""
    ts, n = store.now(), 0
    with store.connect() as c:
        for r in c.execute("SELECT prep_id FROM prep_results WHERE session_id = ?", (session_id,)).fetchall():
            live = c.execute("SELECT COUNT(*) n FROM material_shares WHERE prep_id = ? AND agent_id = ? "
                             "AND status != 'revoked'", (r["prep_id"], agent_id)).fetchone()["n"]
            if live:
                _revoke_tx(c, r["prep_id"], agent_id, ts)
                n += live
    return n


# ---- 열람 (참가자 링크) -------------------------------------------------------------------------

@dataclass
class MaterialView:
    title: str
    slot_text: str
    display_name: str
    mode: str
    result: dict
    expires_at: dt.datetime


def inspect_token(token: str, now: float | None = None) -> MaterialView | None:
    """링크로 자료를 열 수 있으면 화면 데이터를, 아니면 (이유를 구분하지 않고) None."""
    try:
        cl = tokens.verify_material(token, now)
    except tokens.TokenInvalid:
        return None
    with store.connect() as c:
        prep = _prep(c, cl.prep_id)
        if prep is None or prep["session_id"] != cl.session_id:
            return None
        tk = c.execute("SELECT * FROM material_tokens WHERE jti = ?", (cl.jti,)).fetchone()
        if tk is None or tk["revoked_at"] or tk["prep_id"] != cl.prep_id or tk["agent_id"] != cl.agent_id:
            return None
        share = c.execute("SELECT status FROM material_shares WHERE prep_id = ? AND agent_id = ?",
                          (cl.prep_id, cl.agent_id)).fetchone()
        if share is None or share["status"] != "sent":
            return None
        s = c.execute("SELECT * FROM sessions WHERE session_id = ?", (cl.session_id,)).fetchone()
        eff = _effective_session(c, s)
        name = c.execute("SELECT display_name FROM participants WHERE session_id = ? AND agent_id = ?",
                         (eff["session_id"], cl.agent_id)).fetchone()
    if name is None or not is_attendee(cl.session_id, cl.agent_id):
        return None
    from mediator import present
    slot = present._slot_text_of(eff) or ""
    return MaterialView(title=present._title_of(s), slot_text=slot, display_name=name["display_name"],
                        mode=prep["mode"], result=json.loads(prep["result_json"]),
                        expires_at=dt.datetime.fromtimestamp(cl.expires_at))
