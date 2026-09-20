"""자연어 회의 요청 초안: 해석 → 수정(확인 질문 답, 조건 제거·수동 확인) → 확정.

이 모듈의 어떤 함수도 협상을 시작하지 않는다. 협상은 사용자가 '이 조건으로 협상 시작'을
눌렀을 때 server 가 confirm_plan() 을 통과한 계획으로만 시작한다.

초안은 서버에 저장된다. 확정 때 클라이언트가 보낸 값이 아니라 저장된 초안을 결정론적으로
다시 평가한 결과를 쓰므로, 화면에서 값을 바꿔치기해도 검증을 우회할 수 없다.

확정 가능(ready) 조건:
  - 답하지 않은 확인 질문이 없다 (제거된 조건의 질문은 제외)
  - 충돌·검증 실패·알 수 없는 참여자 같은 막는 문제가 없다
  - 자동 처리할 수 없는 조건은 사용자가 '수동 확인'으로 받아들였거나 제거했다
"""
from __future__ import annotations

import datetime as dt
import json
import re
import secrets
from dataclasses import dataclass

from common import config
from common.slots import KST, build_slots
from mediator import constraints as cons
from mediator import nl_parse, people, store
from mediator import policy as policy_mod
from mediator.request_model import Constraint, Issue, MeetingRequest

CHANGE_KEYS = {"answers", "remove", "acknowledge", "unacknowledge", "strengths",
               "priorities", "duration_min", "date_range",
               # 편집 화면
               "title", "purpose", "participants", "required", "approval", "buffer",
               "add_constraints", "update_constraints", "ack_defaults"}
USER_SOURCE = "직접 지정"          # 편집 화면에서 사용자가 직접 넣은 조건의 source_text
DEFAULT_LABEL = {"duration": "회의 시간", "period": "탐색 기간", "participants": "참가자(등록된 전체)"}
EDITABLE_TYPES = ("time_window", "excluded_time", "preferred_time")


class DraftError(Exception):
    """사용자에게 그대로 보일 수 있는 오류. status 는 HTTP 상태 코드에 대응한다."""

    def __init__(self, status: int, message: str, reasons: list | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reasons = reasons or []


@dataclass
class DraftEnv:
    participants: list[dict]                 # [{"agent_id", "name", "is_organizer"}]
    capabilities: dict | None = None         # agent_id -> set[str]
    llm: object | None = None
    now: dt.datetime | None = None
    mode: str = "work"

    def clock(self) -> dt.datetime:
        return self.now or dt.datetime.now(KST)


@dataclass
class ConfirmPlan:
    request: MeetingRequest
    slots: list
    policy: policy_mod.ApprovalPolicy
    effects: dict
    participants: list[dict]


# ---- 평가 ---------------------------------------------------------------------------------

def refresh(req: MeetingRequest, env: DraftEnv):
    """조건을 다시 검증·해석하고 파생 필드(승인 정책 등)를 갱신한다."""
    slots = build_slots(req.date_start, req.mode, req.duration_min, days=req.days)
    selected = people.select(env.participants, req.participant_ids)
    ctx = cons.ResolveContext(slots=slots, participants=selected, now=env.clock(),
                              capabilities=env.capabilities)
    ev = cons.evaluate_request(req, ctx)
    req.issues = [i for i in req.issues if i.origin in ("parse", "parse_people")] + ev.issues
    if req.participant_ids is not None and len(selected) < 2:
        req.issues.append(Issue("invalid", "참가자는 두 명 이상이어야 해요. 참가자를 선택해 주세요.", []))
    total = len(selected)
    req.required = ev.required_names
    p = ev.policy
    req.approval = {"mode": p.mode, "min_count": p.min_count, "min_ratio": p.min_ratio,
                    "needed": p.needed(total), "total": total}
    return ev, slots


def readiness(req: MeetingRequest) -> dict:
    reasons: list[dict] = []
    if req.defaults:
        reasons.append({"kind": "default", "keys": list(req.defaults),
                        "message": "말씀하지 않아 기본값으로 채운 항목을 확인해 주세요: "
                                   + ", ".join(DEFAULT_LABEL.get(k, k) for k in req.defaults)})
    for q in nl_parse.pending_questions(req):
        reasons.append({"kind": "question", "id": q.id, "message": q.text})
    blocked_ids: set[str] = set()
    for i in req.issues:
        if i.blocking:
            blocked_ids.update(i.constraint_ids)
            reasons.append({"kind": i.kind, "message": i.message,
                            "constraint_ids": i.constraint_ids})
    for c in req.active_constraints():
        if not c.supported and not c.acknowledged and c.id not in blocked_ids:
            reasons.append({"kind": "unsupported", "constraint_id": c.id,
                            "message": "자동으로 처리할 수 없는 조건입니다. 직접 확인하겠다고 "
                                       f"표시하거나 제거해 주세요: {c.source_text}"})
    return {"ready": not reasons, "reasons": reasons}


# ---- 화면용 뷰 (개인정보·비밀값 없음) ---------------------------------------------------------------

def _view(draft_id: str, row, req: MeetingRequest, ev, slots, env: DraftEnv) -> dict:
    rd = readiness(req)
    pending = {q.id for q in nl_parse.pending_questions(req)}
    selected = people.select(env.participants, req.participant_ids)
    names = {p["agent_id"]: p["name"] for p in selected}
    host = people.host_of(selected)
    req_names = set(req.required)
    buf = next((c for c in req.constraints if c.active and c.type == "buffer_time"), None)
    return {
        "draftId": draft_id,
        "status": row["status"],
        "parser": row["parser"],
        "expiresAt": row["expires_at"],
        "sessionId": row["session_id"],
        "request": {
            "title": req.title, "purpose": req.purpose,
            "durationMin": req.duration_min,
            "dateStart": req.date_start.isoformat() if req.date_start else None,
            "dateEnd": req.date_end.isoformat() if req.date_end else None,
            "timezone": req.timezone, "priority": req.priority,
            "required": list(req.required),
            "approval": dict(req.approval),
            "approvalDescription": policy_mod.describe(ev.policy, len(selected), names),
            "participants": [p["name"] for p in selected],
        },
        "people": {
            "selected": [{"id": p["agent_id"], "name": p["name"], "isHost": p is host,
                          "required": p["name"] in req_names} for p in selected],
            "available": [{"id": p["agent_id"], "name": p["name"]} for p in env.participants],
            "source": req.participant_source,
            "isDefault": "participants" in req.defaults,
            "host": host["name"] if host else None,
            "hostNote": (f"회의 일정은 {host['name']}님의 캘린더에 만들어져요. (회의를 여는 사람으로서 "
                         "자동으로 참가자가 되는 것은 아니고, 선택한 참가자만 협상에 들어가요.)") if host else None,
        },
        "defaults": [{"key": k, "label": DEFAULT_LABEL.get(k, k)} for k in req.defaults],
        "approvalSpec": dict(req.approval),
        "buffer": ({"before": buf.value.get("before_min", 0), "after": buf.value.get("after_min", 0),
                    "strength": buf.strength, "id": buf.id} if buf else None),
        "constraints": [{
            "id": c.id, "type": c.type, "strength": c.strength, "priority": c.priority,
            "value": c.value, "origin": "user" if c.source_text == USER_SOURCE else "text",
            "sourceText": c.source_text, "supported": c.supported,
            "explanation": c.explanation, "status": c.status,
            "acknowledged": c.acknowledged,
            "excludedSlots": ev.excluded_by.get(c.id, 0),
        } for c in req.constraints],
        "questions": [{
            "id": q.id, "text": q.text, "options": q.options, "answer": q.answer,
            "constraintId": q.constraint_id, "pending": q.id in pending,
        } for q in req.questions],
        "issues": [{"kind": i.kind, "message": i.message, "constraintIds": i.constraint_ids}
                   for i in req.issues],
        "manualChecks": list(ev.manual_checks),
        "stats": {"slots": len(slots), "remaining": ev.remaining},
        "ready": rd["ready"] and row["status"] == "draft" and ev.remaining > 0,
        "blocking": rd["reasons"],
    }


# ---- 저장 ---------------------------------------------------------------------------------

def _load(draft_id: str):
    with store.connect() as c:
        row = c.execute("SELECT * FROM meeting_drafts WHERE draft_id = ?",
                        (draft_id,)).fetchone()
    if row is None:
        raise DraftError(404, "초안을 찾을 수 없습니다")
    return row


def _expire_if_needed(row):
    if row["status"] == "draft" and row["expires_at"] < store.now():
        with store.connect() as c:
            c.execute("UPDATE meeting_drafts SET status = 'expired', updated_at = ? "
                      "WHERE draft_id = ? AND status = 'draft'",
                      (store.now(), row["draft_id"]))
        raise DraftError(410, "초안의 유효 시간이 지났습니다. 다시 입력해 주세요")
    return row


def group_of(draft_id: str) -> str:
    return _load(draft_id)["group_id"]


def owner_of(draft_id: str) -> str:
    return _load(draft_id)["owner"]


def latest_open(owner: str) -> str | None:
    """이 관리자의 가장 최근 편집 중인 초안 (만료·취소·시작된 것은 제외). 새로고침 뒤 복원에 쓴다."""
    if not owner:
        return None
    with store.connect() as c:
        row = c.execute("SELECT draft_id FROM meeting_drafts WHERE owner = ? AND status = 'draft' "
                        "AND expires_at >= ? ORDER BY created_at DESC LIMIT 1",
                        (owner, store.now())).fetchone()
    return row["draft_id"] if row else None


def _save(draft_id: str, req: MeetingRequest) -> None:
    with store.connect() as c:
        c.execute("UPDATE meeting_drafts SET request_json = ?, updated_at = ? "
                  "WHERE draft_id = ? AND status = 'draft'",
                  (json.dumps(req.to_dict(), ensure_ascii=False), store.now(), draft_id))


def _req_of(row) -> MeetingRequest:
    return MeetingRequest.from_dict(json.loads(row["request_json"]))


# ---- 공개 API ---------------------------------------------------------------------------------

def create_draft(text, env: DraftEnv, group_id: str, owner: str = "") -> dict:
    """자연어를 해석해 초안을 저장한다. 협상은 시작하지 않는다.

    Raises: nl_parse.InvalidText — 입력이 비었거나 너무 긺.
    """
    penv = nl_parse.ParseEnv(participants=env.participants, now=env.clock(),
                             capabilities=env.capabilities, llm=env.llm, mode=env.mode)
    req, parser = nl_parse.parse_meeting_text(text, penv)
    refresh(req, env)
    draft_id = "d" + secrets.token_hex(8)
    ts = store.now()
    exp = (dt.datetime.now(dt.timezone.utc)
           + dt.timedelta(minutes=config.DRAFT_TTL_MIN)).isoformat()
    with store.connect() as c:
        c.execute("INSERT INTO meeting_drafts (draft_id, owner, group_id, raw_text, request_json, "
                  "status, parser, created_at, updated_at, expires_at) "
                  "VALUES (?,?,?,?,?,'draft',?,?,?,?)",
                  (draft_id, owner, group_id, req.source_text,
                   json.dumps(req.to_dict(), ensure_ascii=False), parser, ts, ts, exp))
    return get_view(draft_id, env)


def get_view(draft_id: str, env: DraftEnv) -> dict:
    row = _load(draft_id)
    req = _req_of(row)
    ev, slots = refresh(req, env)
    return _view(draft_id, row, req, ev, slots, env)


def resolve_draft(draft_id: str, changes: dict, env: DraftEnv) -> dict:
    """확인 질문 답, 조건 제거, 수동 확인, 강도/우선순위/소요 시간/기간 수정을 반영한다."""
    if not isinstance(changes, dict):
        raise DraftError(400, "요청 형식이 올바르지 않습니다")
    unknown = set(changes) - CHANGE_KEYS
    if unknown:
        raise DraftError(400, f"알 수 없는 필드: {', '.join(sorted(map(str, unknown)))}")
    row = _expire_if_needed(_load(draft_id))
    if row["status"] != "draft":
        raise DraftError(409, "이미 시작되었거나 취소된 초안입니다")
    req = _req_of(row)

    def _cons(cid):
        c = req.get(cid) if isinstance(cid, str) else None
        if c is None:
            raise DraftError(400, f"알 수 없는 조건입니다: {str(cid)[:20]}")
        return c

    answers = changes.get("answers") or {}
    if not isinstance(answers, dict):
        raise DraftError(400, "answers 형식이 올바르지 않습니다")
    for qid, value in answers.items():
        try:
            nl_parse.apply_answer(req, qid, value)
        except KeyError as exc:
            raise DraftError(400, str(exc.args[0])) from None
        except ValueError as exc:
            raise DraftError(400, str(exc)) from None
    for cid in changes.get("remove") or []:
        _cons(cid).status = "removed"
    for cid in changes.get("acknowledge") or []:
        c = _cons(cid)
        if c.active and not c.supported:
            c.acknowledged = True
    for cid in changes.get("unacknowledge") or []:
        _cons(cid).acknowledged = False
    for cid, strength in (changes.get("strengths") or {}).items():
        c = _cons(cid)
        if strength not in ("hard", "soft"):
            raise DraftError(400, "강도는 hard 또는 soft 여야 합니다")
        c.strength = strength
        if c.type == "time_window" and strength == "soft":
            c.type = "preferred_time"
    for cid, pr in (changes.get("priorities") or {}).items():
        c = _cons(cid)
        if not isinstance(pr, int) or isinstance(pr, bool) or not 1 <= pr <= 5:
            raise DraftError(400, "우선순위는 1~5 정수여야 합니다")
        c.priority = pr
    if "duration_min" in changes:
        d = changes["duration_min"]
        if (not isinstance(d, int) or isinstance(d, bool)
                or not nl_parse.DURATION_MIN <= d <= nl_parse.DURATION_MAX or d % 5):
            raise DraftError(400, f"소요 시간은 {nl_parse.DURATION_MIN}~"
                                  f"{nl_parse.DURATION_MAX}분 사이의 5분 단위여야 합니다")
        req.duration_min = d
        _clear_default(req, "duration")
        for q in req.questions:
            if q.kind == "duration_choice" and q.answer is None:
                q.answer = d
    if "date_range" in changes:
        dr = changes["date_range"]
        if not isinstance(dr, dict):
            raise DraftError(400, "기간 형식이 올바르지 않습니다")
        s, e, issues = nl_parse.resolve_date_range(
            {"anchor": "explicit", "start": dr.get("start"), "end": dr.get("end")},
            env.clock().date())
        if issues:
            raise DraftError(400, issues[0].message)
        req.date_start, req.date_end = s, e
        _clear_default(req, "period")

    _apply_edits(req, changes, env)

    ev, slots = refresh(req, env)
    _save(draft_id, req)
    return _view(draft_id, _load(draft_id), req, ev, slots, env)


def _clear_default(req: MeetingRequest, key: str) -> None:
    req.defaults = [k for k in req.defaults if k != key]


def _text(v, limit: int, what: str) -> str:
    if not isinstance(v, str):
        raise DraftError(400, f"{what} 형식이 올바르지 않습니다")
    return re.sub(r"[\x00-\x1f]", " ", v).strip()[:limit]


def _retire(req: MeetingRequest, ctype: str) -> None:
    for c in req.constraints:
        if c.type == ctype and c.active:
            c.status = "removed"


def _add(req: MeetingRequest, ctype: str, value: dict, strength: str, priority: int) -> Constraint:
    c = Constraint(id=req.next_constraint_id(), type=ctype, value=value, strength=strength,
                   priority=priority, source_text=USER_SOURCE)
    req.constraints.append(c)
    return c


def _check_value(ctype: str, value) -> dict:
    """시간 조건 값의 모양만 서버가 확인한다 (의미 검증은 evaluate 가 다시 한다)."""
    if not isinstance(value, dict):
        raise DraftError(400, "시간 조건 형식이 올바르지 않습니다")
    out: dict = {}
    wds = value.get("weekdays")
    if wds is not None:
        if (not isinstance(wds, list) or len(wds) > 7
                or any(not isinstance(x, int) or isinstance(x, bool) or not 0 <= x <= 6 for x in wds)):
            raise DraftError(400, "요일은 0(월)~6(일) 정수 목록이어야 합니다")
        out["weekdays"] = sorted(set(wds))
    dates = value.get("dates")
    if dates is not None:
        try:
            out["dates"] = [dt.date.fromisoformat(d).isoformat() for d in dates][:31]
        except (TypeError, ValueError):
            raise DraftError(400, "날짜 형식이 올바르지 않습니다") from None
    for k in ("start_time", "end_time"):
        t = value.get(k)
        if t in (None, ""):
            continue
        if not isinstance(t, str) or not re.fullmatch(r"(?:[01]\d|2[0-4]):[0-5]\d", t):
            raise DraftError(400, "시각은 HH:MM 형식이어야 합니다")
        out[k] = t
    if not out:
        raise DraftError(400, "요일이나 시각을 하나 이상 지정해 주세요")
    out["label"] = _text(value.get("label", ""), 20, "이름") or {
        "time_window": "필수 시간대", "excluded_time": "제외 시간", "preferred_time": "선호 시간"}[ctype]
    return out


def _apply_edits(req: MeetingRequest, changes: dict, env: DraftEnv) -> None:
    """편집 화면의 변경을 초안에 반영한다. 값은 모두 서버가 다시 검증한다."""
    by_id = {p["agent_id"]: p for p in env.participants}
    by_name = {p["name"]: p for p in env.participants}

    if "title" in changes:
        req.title = _text(changes["title"], 60, "제목") or "FairMeet 협의 일정"
    if "purpose" in changes:
        req.purpose = _text(changes["purpose"], 200, "목적")

    if "participants" in changes:
        ids = changes["participants"]
        if (not isinstance(ids, list) or len(ids) > 30
                or any(not isinstance(i, str) or i not in by_id for i in ids)):
            raise DraftError(400, "등록되지 않은 참가자가 포함되어 있어요")
        order = {p["agent_id"]: k for k, p in enumerate(env.participants)}
        req.participant_ids = sorted(dict.fromkeys(ids), key=lambda i: order[i])
        req.participant_source = "user"
        _clear_default(req, "participants")
        req.issues = [i for i in req.issues if i.origin != "parse_people"]   # 직접 고른 뒤에는 문장 해석의 참가자 안내가 낡았다

    if "required" in changes:
        ids = changes["required"]
        if not isinstance(ids, list) or any(not isinstance(i, str) or i not in by_id for i in ids):
            raise DraftError(400, "필수 참석자는 등록된 참가자여야 해요")
        _retire(req, "required_participant")
        if ids:
            _add(req, "required_participant", {"names": [by_id[i]["name"] for i in dict.fromkeys(ids)]},
                 "hard", 3)

    if "approval" in changes:
        a = changes["approval"]
        if not isinstance(a, dict) or a.get("mode") not in ("all", "min_count", "min_ratio"):
            raise DraftError(400, "승인 방식은 전원 / 최소 인원 / 최소 비율 중에서 골라 주세요")
        _retire(req, "quorum")
        if a["mode"] == "min_count":
            n = a.get("count")
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 30:
                raise DraftError(400, "최소 승인 인원은 1 이상의 정수여야 해요")
            _add(req, "quorum", {"kind": "min_count", "count": n, "scope": "total"}, "hard", 3)
        elif a["mode"] == "min_ratio":
            r = a.get("ratio")
            if not isinstance(r, (int, float)) or isinstance(r, bool) or not 0 < r <= 1:
                raise DraftError(400, "최소 승인 비율은 0보다 크고 1 이하여야 해요")
            _add(req, "quorum", {"kind": "ratio", "ratio": float(r)}, "hard", 3)

    if "buffer" in changes:
        b = changes["buffer"]
        _retire(req, "buffer_time")
        if b:
            if not isinstance(b, dict):
                raise DraftError(400, "여유 시간 형식이 올바르지 않습니다")
            before, after = b.get("before", 0), b.get("after", 0)
            for v in (before, after):
                if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 120:
                    raise DraftError(400, "여유 시간은 0~120분이어야 해요")
            if before or after:
                if b.get("strength", "soft") not in ("hard", "soft"):
                    raise DraftError(400, "여유 시간 강도는 필수/선호 중에서 골라 주세요")
                _add(req, "buffer_time", {"before_min": before, "after_min": after, "participants": []},
                     b.get("strength", "soft"), 3)

    for item in changes.get("add_constraints") or []:
        if (not isinstance(item, dict) or item.get("type") not in EDITABLE_TYPES
                or item.get("strength", "soft") not in ("hard", "soft")):
            raise DraftError(400, "추가할 시간 조건 형식이 올바르지 않습니다")
        pr = item.get("priority", 3)
        if not isinstance(pr, int) or isinstance(pr, bool) or not 1 <= pr <= 5:
            raise DraftError(400, "우선순위는 1~5 정수여야 합니다")
        ctype, strength = item["type"], item.get("strength", "soft")
        if ctype == "time_window" and strength == "soft":
            ctype = "preferred_time"
        if len([c for c in req.constraints if c.active]) >= nl_parse.MAX_CONSTRAINTS:
            raise DraftError(400, "조건이 너무 많아요")
        _add(req, ctype, _check_value(ctype, item.get("value")), strength, pr)

    for cid, upd in (changes.get("update_constraints") or {}).items():
        c = req.get(cid) if isinstance(cid, str) else None
        if c is None or not isinstance(upd, dict):
            raise DraftError(400, "알 수 없는 조건입니다")
        if "value" in upd:
            if c.type not in EDITABLE_TYPES:
                raise DraftError(400, "이 조건은 값을 직접 고칠 수 없어요")
            c.value = _check_value(c.type, upd["value"])
        if "strength" in upd:
            if upd["strength"] not in ("hard", "soft"):
                raise DraftError(400, "강도는 hard 또는 soft 여야 합니다")
            c.strength = upd["strength"]
            if c.type == "time_window" and c.strength == "soft":
                c.type = "preferred_time"
        if "priority" in upd:
            pr = upd["priority"]
            if not isinstance(pr, int) or isinstance(pr, bool) or not 1 <= pr <= 5:
                raise DraftError(400, "우선순위는 1~5 정수여야 합니다")
            c.priority = pr

    for key in changes.get("ack_defaults") or []:
        if key not in DEFAULT_LABEL:
            raise DraftError(400, "알 수 없는 항목입니다")
        _clear_default(req, key)


def cancel_draft(draft_id: str) -> bool:
    row = _load(draft_id)
    with store.connect() as c:
        cur = c.execute("UPDATE meeting_drafts SET status = 'cancelled', updated_at = ? "
                        "WHERE draft_id = ? AND status = 'draft'", (store.now(), draft_id))
    return cur.rowcount > 0 or row["status"] == "cancelled"


def confirm_plan(draft_id: str, env: DraftEnv) -> ConfirmPlan:
    """확정 직전 검증. 통과해야만 협상을 시작할 수 있다. 저장된 초안을 다시 평가한다."""
    row = _expire_if_needed(_load(draft_id))
    if row["status"] == "started":
        raise DraftError(409, "이미 협상이 시작된 초안입니다")
    if row["status"] != "draft":
        raise DraftError(409, "취소되었거나 만료된 초안입니다")
    req = _req_of(row)
    ev, slots = refresh(req, env)
    rd = readiness(req)
    if not rd["ready"]:
        raise DraftError(409, "아직 확인이 필요한 항목이 있습니다", rd["reasons"])
    if ev.remaining <= 0:
        raise DraftError(409, "조건을 만족하는 시간이 없습니다")
    _save(draft_id, req)                    # 확정 시점의 해석을 남긴다
    # 협상에 들어가는 참가자는 사용자가 확인한 선택 그대로 (스냅샷으로 세션에 저장된다)
    return ConfirmPlan(request=req, slots=slots, policy=ev.policy,
                       effects=ev.slot_effects(),
                       participants=people.select(env.participants, req.participant_ids))


def claim_draft(draft_id: str, session_id: str) -> bool:
    """초안을 '시작됨'으로 원자적으로 표시한다. 더블 클릭·동시 요청 중 하나만 성공한다."""
    with store.connect() as c:
        cur = c.execute(
            "UPDATE meeting_drafts SET status = 'started', session_id = ?, updated_at = ? "
            "WHERE draft_id = ? AND status = 'draft' AND expires_at >= ?",
            (session_id, store.now(), draft_id, store.now()))
    return cur.rowcount == 1


def release_draft(draft_id: str) -> None:
    """세션을 만들지 못했을 때 초안을 다시 열어 준다."""
    with store.connect() as c:
        c.execute("UPDATE meeting_drafts SET status = 'draft', session_id = NULL, "
                  "updated_at = ? WHERE draft_id = ? AND status = 'started'",
                  (store.now(), draft_id))
