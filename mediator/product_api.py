"""제품 화면(/)이 쓰는 API: 로그인, 자연어 회의 요청(편집 가능한 초안), 회의 목록·상세, 변경 요청 처리, 회의 자료, 시연 모드.

모든 경로는 mediator.security 의 관문(기본 거부)을 지난다. 여기서 공개인 것은 /api/me 와 /api/login 뿐이다.
서버 본체(server.py)와의 순환 참조를 피하려고 필요한 함수는 configure() 로 주입받는다.

회의 요청 흐름 (v5): 자연어 입력 → 해석 → 편집 가능한 초안 → 사용자가 조건을 확인·수정 → '회의 요청 확정' → 협상 시작.
자연어 입력만으로는 협상도 이메일도 시작되지 않는다.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from types import SimpleNamespace

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from common import config
from mediator import auth, change_flow, drafts, guard, materials, nl_parse, present, security, store
from mediator import llm as llm_mod

log = logging.getLogger("fairmeet.product")
router = APIRouter()

deps = SimpleNamespace(group_id="demo-team", participants=None, draft_env=None,
                       confirm_and_start=None, agent_names=None, spawn=None)


def configure(*, group_id, participants, draft_env, confirm_and_start, agent_names, spawn=None) -> None:
    deps.group_id, deps.participants, deps.draft_env = group_id, participants, draft_env
    deps.confirm_and_start, deps.agent_names, deps.spawn = confirm_and_start, agent_names, spawn


def _msg(status: int, message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message, **extra})


# ---- 세션 · 로그인 --------------------------------------------------------------------------------

@router.get("/api/me")
def me(request: Request):
    p = auth.authenticate(request)
    if p is None:
        return {"authenticated": False, "loginRequired": True}
    return {"authenticated": True, "user": p.user if p.authenticated else "이 컴퓨터",
            "mode": "admin" if p.authenticated else "local",
            "csrfToken": p.csrf, "demoMode": p.demo, "demoAvailable": p.authenticated}


@router.post("/api/login")
def login(request: Request, payload: dict = Body(default={})):
    # 다른 사이트에서 브라우저를 시켜 보낸 로그인 시도는 받지 않는다 (Origin + 사용자 지정 헤더)
    if not auth.origin_ok(request) or request.headers.get("x-fairmeet") != "1":
        security.audit("csrf", route="/api/login")
        return _msg(403, "요청을 확인하지 못했어요. 화면을 새로고침한 뒤 다시 시도해 주세요.")
    ip = auth.client_ip(request)
    user = payload.get("username") if isinstance(payload, dict) else None
    pw = payload.get("password") if isinstance(payload, dict) else None
    if not isinstance(user, str) or not isinstance(pw, str) or len(user) > 100 or len(pw) > 200:
        return _msg(400, "아이디와 비밀번호를 입력해 주세요.")
    res = auth.login(user, pw, ip)
    if not res.ok:
        security.audit("rate_limited" if res.retry_after else "login_failed", category="login",
                       actor=ip, route="/api/login")
        return JSONResponse(status_code=429 if res.retry_after else 401,
                            content={"detail": res.message},
                            headers={"Retry-After": str(res.retry_after)} if res.retry_after else None)
    security.audit("login_ok", category="login", actor=res.session.user, route="/api/login")
    resp = JSONResponse({"authenticated": True, "user": res.session.user, "mode": "admin",
                         "csrfToken": res.session.csrf, "demoMode": False, "demoAvailable": True})
    auth.set_session_cookie(resp, request, res.session)
    return resp


@router.post("/api/logout")
def logout(request: Request):
    p = security.principal_of(request)
    if p.session:
        auth.SESSIONS.destroy(p.session.sid)
    resp = JSONResponse({"ok": True})
    auth.clear_session_cookie(resp)
    return resp


@router.post("/api/demo-mode")
def demo_mode(request: Request, payload: dict = Body(default={})):
    p = security.principal_of(request)
    if not p.authenticated or p.session is None:
        security.audit("forbidden", category="demo_mode", actor=p.key, route="/api/demo-mode")
        return _msg(403, "시연 모드는 관리자로 로그인한 뒤에 켤 수 있어요.")
    p.session.demo = bool(payload.get("enabled")) if isinstance(payload, dict) else False
    return {"demoMode": p.session.demo}


def require_demo(request: Request) -> None:
    """시연 모드에서만 보이는 데이터(양보 잔액, 협상 과정, 원시 로그)의 서버 측 확인."""
    p = security.principal_of(request)
    if not p.demo:
        raise HTTPException(403, "시연 모드에서만 볼 수 있어요.")


# ---- 자연어 회의 요청 → 편집 가능한 초안 ----------------------------------------------------------------

TYPE_LABEL = {"time_window": "필수 시간대", "excluded_time": "제외할 시간", "preferred_time": "선호 시간",
              "earliest_first": "빠른 시간 우선", "required_participant": "필수 참석자",
              "quorum": "승인 기준", "buffer_time": "전후 여유", "meeting_mode": "회의 방식",
              "location": "장소", "recurrence": "반복", "accessibility": "접근성",
              "external_context": "외부 조건", "other": "해석하지 못한 조건"}
EDITABLE = ("time_window", "excluded_time", "preferred_time")
HIDDEN_IN_EDITOR = ("required_participant", "quorum", "buffer_time")     # 전용 입력란이 따로 있다
_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _period(rq: dict) -> str | None:
    if not rq.get("dateStart") or not rq.get("dateEnd"):
        return None
    import datetime as dt
    a, b = dt.date.fromisoformat(rq["dateStart"]), dt.date.fromisoformat(rq["dateEnd"])
    return f"{a:%m월 %d일} ~ {b:%m월 %d일}"


def _handle(draft_id: str, agent_id: str) -> str:
    """화면에는 내부 agent id 대신 초안마다 다른 불투명한 참가자 표식을 준다."""
    return "p" + hashlib.sha256(f"{draft_id}:{agent_id}".encode("utf-8")).hexdigest()[:10]


def _agent_ids(draft_id: str, handles) -> list:
    if not isinstance(handles, list):
        return handles
    back = {_handle(draft_id, p["agent_id"]): p["agent_id"] for p in deps.participants(deps.group_id)}
    return [back.get(h, "?") if isinstance(h, str) else h for h in handles]


def friendly_draft(v: dict) -> dict:
    """초안 화면용 뷰. 기본값으로 채운 항목(defaults)과 직접 고칠 수 있는 값을 함께 담는다."""
    rq = v["request"]
    ppl = dict(v["people"])
    for key in ("selected", "available"):
        ppl[key] = [{**x, "id": _handle(v["draftId"], x["id"])} for x in ppl[key]]
    default_keys = {d["key"] for d in v.get("defaults", [])}
    cons = []
    for c in v["constraints"]:
        if c["status"] == "removed" or c["type"] in HIDDEN_IN_EDITOR:
            continue
        cons.append({"id": c["id"], "label": TYPE_LABEL.get(c["type"], "조건"),
                     "strength": "필수" if c["strength"] == "hard" else "선호",
                     "hard": c["strength"] == "hard", "type": c["type"], "priority": c["priority"],
                     "text": c["explanation"] or c["sourceText"], "source": c["sourceText"],
                     "value": c["value"] if c["type"] in EDITABLE else None,
                     "supported": c["supported"], "acknowledged": c["acknowledged"],
                     "origin": c.get("origin", "text"),
                     "editable": c["type"] in EDITABLE, "canToggle": c["supported"] and c["type"] in EDITABLE})
    return {
        "draftId": v["draftId"], "status": v["status"], "ready": v["ready"], "expiresAt": v["expiresAt"],
        "meetingId": v.get("sessionId"),
        "summary": {"durationMin": rq["durationMin"], "period": _period(rq),
                    "range": {"start": rq.get("dateStart"), "end": rq.get("dateEnd")},
                    "participants": rq["participants"], "required": rq["required"],
                    "approval": rq["approvalDescription"], "candidates": v["stats"]["remaining"],
                    "title": rq.get("title"), "purpose": rq.get("purpose") or ""},
        "people": ppl,
        "approvalSpec": v["approvalSpec"], "buffer": v["buffer"],
        "defaults": [{"key": d["key"], "label": d["label"]} for d in v.get("defaults", [])],
        "filled": {"duration": "duration" in default_keys, "period": "period" in default_keys,
                   "participants": "participants" in default_keys},
        "constraints": cons,
        "unrecognized": [c for c in cons if c["type"] == "other"],
        "questions": [{"id": q["id"], "text": q["text"], "options": q["options"],
                       "answer": q["answer"], "pending": q["pending"]} for q in v["questions"]],
        "issues": [{"kind": i["kind"], "message": i["message"]} for i in v["issues"]],
        "blocking": [{"message": b["message"]} for b in v["blocking"]],
    }


def _draft_error(exc: drafts.DraftError) -> JSONResponse:
    body = {"detail": exc.message}
    if exc.reasons:
        body["reasons"] = [{"message": r.get("message", "")} for r in exc.reasons]
    return JSONResponse(status_code=exc.status, content=body)


def _text_hash(text: str) -> str:
    return hashlib.sha256(guard.normalize(text).encode("utf-8")).hexdigest()[:32]


def _claim_key(scope: str, key: str, req_hash: str) -> str:
    """new | replay | conflict | busy — 같은 요청이 두 번 처리되지 않게 원자적으로 자리를 잡는다."""
    with store.connect() as c:
        row = c.execute("SELECT * FROM request_keys WHERE scope = ? AND key = ?",
                        (scope, key)).fetchone()
        if row is None:
            c.execute("INSERT INTO request_keys (scope, key, request_hash, status, created_at) "
                      "VALUES (?,?,?, 'processing', ?)", (scope, key, req_hash, store.now()))
            return "new"
        if row["request_hash"] != req_hash:
            return "conflict"
        return "replay" if row["status"] == "done" else "busy"


def _finish_key(scope: str, key: str, outcome: str, draft_id=None, session_id=None) -> None:
    with store.connect() as c:
        c.execute("UPDATE request_keys SET status = 'done', outcome = ?, draft_id = ?, session_id = ? "
                  "WHERE scope = ? AND key = ?", (outcome, draft_id, session_id, scope, key))


def _drop_key(scope: str, key: str) -> None:
    with store.connect() as c:
        c.execute("DELETE FROM request_keys WHERE scope = ? AND key = ? AND status = 'processing'",
                  (scope, key))


def _key_row(scope: str, key: str):
    with store.connect() as c:
        return c.execute("SELECT * FROM request_keys WHERE scope = ? AND key = ?", (scope, key)).fetchone()


def _refusal_payload(g: guard.GuardResult) -> dict:
    return {"refusals": [{"message": r.message, "alternative": r.alternative} for r in g.refusals],
            "notes": list(g.notes)}


def _started(meeting_id: str, draft_id: str | None, replayed: bool = False) -> dict:
    return {"status": "started", "meetingId": meeting_id, "draftId": draft_id,
            "message": "회의를 요청했어요. 참가자의 일정을 확인하고 있어요.", "replayed": replayed}


def _guard_only(g: guard.GuardResult) -> dict:
    if g.intent == "blocked":
        first = g.refusals[0] if g.refusals else None
        return {"status": "refused", "message": first.message if first else "이 요청은 처리할 수 없어요.",
                "alternative": first.alternative if first else "", **_refusal_payload(g)}
    return {"status": "question",
            "message": "회의 요청으로 보이지 않아요. 날짜, 참석자, 시간 같은 조건을 함께 입력해 주세요.",
            "alternative": "예: 다음 주 화요일 오후에 재원, 도연과 90분 회의를 잡아줘.", **_refusal_payload(g)}


def _owned(draft_id: str, p: auth.Principal) -> None:
    """다른 관리자가 만든 초안은 존재하지 않는 것처럼 답한다."""
    owner = drafts.owner_of(draft_id)
    if not owner or owner != p.key:
        raise drafts.DraftError(404, "초안을 찾을 수 없습니다")


@router.post("/api/requests")
def create_request(request: Request, payload: dict = Body(default={})):
    """자연어 회의 요청을 해석해 편집 가능한 초안을 만든다. 협상도 이메일도 시작하지 않는다.

    idempotencyKey(또는 Idempotency-Key 헤더)가 같은 요청은 한 번만 처리되고 같은 초안을 돌려준다."""
    p = security.principal_of(request)
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        return _msg(400, "회의 요청 문장을 입력해 주세요.")
    text = payload["text"]
    if not text.strip():
        return _msg(400, "회의 요청 문장을 입력해 주세요.")
    if len(text) > config.NL_MAX_CHARS:
        return _msg(400, f"요청이 너무 길어요. {config.NL_MAX_CHARS}자 이내로 줄여 주세요.")

    wait = auth.ABUSE.blocked(p.key)
    if wait or not auth.MEETING_REQUESTS.allow(p.key):
        security.audit("rate_limited", category="requests", actor=p.key, route="/api/requests")
        return JSONResponse(status_code=429, headers={"Retry-After": str(wait or 60)},
                            content={"status": "rate_limited",
                                     "detail": "요청이 너무 잦아요. 잠시 후 다시 시도해 주세요."})

    key = payload.get("idempotencyKey") or request.headers.get("idempotency-key")
    if key is not None and (not isinstance(key, str) or not _KEY_RE.match(key)):
        return _msg(400, "요청 식별값 형식이 올바르지 않아요.")
    scope, req_hash = p.key, _text_hash(text)

    names = tuple(pt["name"] for pt in deps.participants(deps.group_id))
    g = guard.inspect(text, names)

    if key:
        state = _claim_key(scope, key, req_hash)
        if state == "conflict":
            return _msg(409, "같은 요청 식별값에 다른 내용이 들어왔어요. 새로 입력해 주세요.")
        deadline = time.time() + 20
        while state == "busy" and time.time() < deadline:      # 같은 요청이 처리 중이면 끝나기를 기다린다
            time.sleep(0.2)
            row = _key_row(scope, key)
            state = "replay" if row and row["status"] == "done" else ("new" if row is None else "busy")
            if state == "new":
                state = _claim_key(scope, key, req_hash)
        if state == "busy":
            return _msg(409, "같은 요청을 처리하고 있어요. 잠시 후 확인해 주세요.")
        if state == "replay":
            return _replay(scope, key, g, p)
    try:
        result = _process(p, text, g)
    except BaseException:
        if key:
            _drop_key(scope, key)
        raise
    if key:
        _finish_key(scope, key, result["status"], result.get("draftId"), result.get("meetingId"))
    return result


def _replay(scope: str, key: str, g: guard.GuardResult, p: auth.Principal):
    row = _key_row(scope, key)
    if row["outcome"] == "draft" and row["draft_id"]:
        try:
            _owned(row["draft_id"], p)
            v = drafts.get_view(row["draft_id"], deps.draft_env(deps.group_id))
        except drafts.DraftError as exc:
            return _draft_error(exc)
        if v["status"] == "started" and v.get("sessionId"):
            return _started(v["sessionId"], row["draft_id"], replayed=True)
        return {"status": "draft", "draftId": row["draft_id"], "draft": friendly_draft(v),
                "message": _draft_message(g), "replayed": True, **_refusal_payload(g)}
    return _guard_only(g) | {"replayed": True}


def _draft_message(g: guard.GuardResult) -> str:
    if g.refusals:
        return "요청하신 내용 중 처리할 수 없는 부분은 제외했어요. 남은 조건을 확인해 주세요."
    return "이렇게 이해했어요. 확인하고 필요하면 고친 뒤 요청해 주세요."


def _process(p: auth.Principal, text: str, g: guard.GuardResult) -> dict:
    if g.refusals:
        security.audit("blocked_input", category=",".join(r.category for r in g.refusals),
                       actor=p.key, route="/api/requests", text=text)
        auth.ABUSE.fail(p.key)
    if g.intent in ("blocked", "question", "empty"):
        return _guard_only(g)
    env = deps.draft_env(deps.group_id)
    try:
        view = drafts.create_draft(g.text, env, deps.group_id, owner=p.key)
    except nl_parse.InvalidText as exc:
        return {"status": "question", "message": str(exc), "alternative": "", **_refusal_payload(g)}
    message = _draft_message(g)
    if view["parser"] == "rules" and llm_mod.get_llm() is not None:
        message = "AI 해석을 사용하지 못해 규칙 기반으로 해석했어요. " + message
    return {"status": "draft", "draftId": view["draftId"], "draft": friendly_draft(view),
            "message": message, **_refusal_payload(g)}


@router.get("/api/requests/current")
def current_request(request: Request):
    """새로고침 뒤에도 편집 중이던 초안을 복원한다 (이 관리자의 초안만)."""
    p = security.principal_of(request)
    did = drafts.latest_open(p.key)
    if not did:
        return {"draft": None}
    try:
        v = drafts.get_view(did, deps.draft_env(drafts.group_of(did)))
    except drafts.DraftError:
        return {"draft": None}
    return {"status": "draft", "draftId": did, "draft": friendly_draft(v)}


@router.get("/api/requests/{draft_id}")
def get_request(draft_id: str, request: Request):
    p = security.principal_of(request)
    try:
        _owned(draft_id, p)
        v = drafts.get_view(draft_id, deps.draft_env(drafts.group_of(draft_id)))
    except drafts.DraftError as exc:
        return _draft_error(exc)
    if v["status"] == "started" and v.get("sessionId"):
        return _started(v["sessionId"], draft_id)
    return {"status": "draft", "draftId": draft_id, "draft": friendly_draft(v)}


@router.post("/api/requests/{draft_id}/resolve")
def resolve_request(draft_id: str, request: Request, payload: dict = Body(default={})):
    """조건 확인·수정. 바뀔 때마다 충돌·정족수·날짜를 다시 검증한 결과를 돌려준다."""
    p = security.principal_of(request)
    try:
        _owned(draft_id, p)
        if isinstance(payload, dict):
            payload = dict(payload)
            for key in ("participants", "required"):
                if key in payload:
                    payload[key] = _agent_ids(draft_id, payload[key])
        v = drafts.resolve_draft(draft_id, payload, deps.draft_env(drafts.group_of(draft_id)))
    except drafts.DraftError as exc:
        return _draft_error(exc)
    return {"status": "draft", "draftId": draft_id, "draft": friendly_draft(v)}


@router.post("/api/requests/{draft_id}/confirm")
def confirm_request(draft_id: str, request: Request):
    """'회의 요청 확정'. 저장된 초안을 서버가 다시 검증해 통과해야만 협상이 시작된다.

    이미 이 초안으로 시작된 회의가 있으면 (더블 클릭·재시도) 같은 회의를 돌려주고 새로 만들지 않는다."""
    p = security.principal_of(request)
    try:
        _owned(draft_id, p)
        row = drafts._load(draft_id)
        if row["status"] == "started" and row["session_id"]:
            return _started(row["session_id"], draft_id, replayed=True)
        res = deps.confirm_and_start(draft_id)
    except drafts.DraftError as exc:
        if exc.status == 409:                                    # 동시에 들어온 다른 확정이 먼저 시작했다
            row = drafts._load(draft_id)
            if row["status"] == "started" and row["session_id"]:
                return _started(row["session_id"], draft_id, replayed=True)
        return _draft_error(exc)
    return _started(res["sessionId"], draft_id)


@router.post("/api/requests/{draft_id}/cancel")
def cancel_request(draft_id: str, request: Request):
    p = security.principal_of(request)
    try:
        _owned(draft_id, p)
        return {"cancelled": drafts.cancel_draft(draft_id)}
    except drafts.DraftError as exc:
        return _draft_error(exc)


# ---- 회의 목록 · 상세 ------------------------------------------------------------------------------

@router.get("/api/meetings")
def meetings(group: str = "all"):
    if group not in ("all", "progress", "waiting", "confirmed", "failed", "cancelled"):
        return _msg(400, "알 수 없는 필터예요.")
    return {"meetings": present.scrub(present.list_meetings(group))}


@router.get("/api/meetings/{meeting_id}")
def meeting(meeting_id: str):
    d = present.meeting_detail(meeting_id)
    if d is None:
        return _msg(404, "회의를 찾을 수 없어요.")
    d["materials"] = present.scrub(materials.list_for_session(meeting_id))
    return d


# ---- 변경 요청 (참가자가 보낸 요청을 주최자가 처리한다) --------------------------------------------------

@router.get("/api/change-requests")
def open_change_requests():
    """확인이 필요한 변경 요청 (모든 회의). 홈 화면의 알림에 쓴다."""
    change_flow.expire_stale_changes()
    change_flow.expire_stale_intakes()
    return {"requests": present.scrub(change_flow.intake_cards(only_open=True))}


@router.post("/api/change-requests/{intake_id}/decide")
def decide_change_request(intake_id: str, request: Request, payload: dict = Body(default={})):
    """주최자가 처리 방법을 고른다: renegotiate | proceed_without | cancel | decline.

    고르기 전에는 다른 참가자에게 아무것도 가지 않는다. 같은 요청을 두 번 결정할 수 없다."""
    p = security.principal_of(request)
    action = payload.get("action") if isinstance(payload, dict) else None
    card = next((i for i in change_flow.intake_cards(only_open=False) if i["intakeId"] == intake_id), None)
    res = change_flow.decide_intake(intake_id, action if isinstance(action, str) else "", actor="organizer")
    security.audit("change_decision", category=str(action)[:20], actor=p.key, route="/api/change-requests")
    if res.status == "created":
        change_flow.run_created_hook(res)                        # 투표 메일 발송 (백그라운드)
        return {"status": "voting", "message": res.message}
    if res.status == "declined":
        if card and deps.spawn:
            deps.spawn(lambda: change_flow.dispatch_mail(card["meetingId"], intake_id, ("result",)))
        return {"status": "declined", "message": res.message}
    return _msg(409 if res.status == "rejected" else 404, res.message or "처리할 수 없어요.")


# ---- 시연 모드 (서버가 다시 확인한다) ----------------------------------------------------------------

@router.get("/api/demo/ledger")
def demo_ledger(request: Request):
    require_demo(request)
    return present.demo_ledger(deps.group_id, deps.agent_names())


@router.get("/api/demo/{meeting_id}")
def demo_meeting(meeting_id: str, request: Request):
    require_demo(request)
    v = present.demo_view(meeting_id, deps.agent_names())
    if v is None:
        return _msg(404, "회의를 찾을 수 없어요.")
    return v


# ---- 회의 자료 ------------------------------------------------------------------------------------

@router.get("/api/materials/status")
def materials_status():
    llm = llm_mod.get_llm(model=config.PREP_MODEL) is not None
    return {"enabled": config.PREP_ENABLED, "aiAvailable": llm,
            "modeText": ("AI 다중 에이전트 분석을 사용할 수 있어요." if llm else
                         "AI 분석이 설정되어 있지 않아 규칙 기반의 제한적인 정리만 제공해요."),
            "limits": {"maxMb": round(config.PREP_MAX_BYTES / 1024 / 1024, 1),
                       "maxPages": config.PREP_MAX_PAGES}}


@router.post("/api/meetings/{meeting_id}/materials")
def upload_material(meeting_id: str, file: UploadFile = File(...)):
    from prep import pipeline, rules
    from prep.pdf_reader import PdfRejected, read_pdf
    if not config.PREP_ENABLED:
        return _msg(404, "회의 자료 기능이 꺼져 있어요.")
    if store.get_session(meeting_id) is None:
        return _msg(404, "회의를 찾을 수 없어요.")
    limit = config.PREP_MAX_BYTES
    data = file.file.read(limit + 1)
    if len(data) > limit:
        return _msg(413, f"파일이 너무 커요. 최대 {limit // 1024 // 1024}MB까지 올릴 수 있어요.")
    try:
        doc = read_pdf(data, file.filename or "")
    except PdfRejected as exc:
        return _msg(exc.status, exc.message)
    llm = llm_mod.get_llm(model=config.PREP_MODEL)
    mode, notice = "rules", None
    result = None
    if llm is not None:
        try:
            result = pipeline.analyze(doc, llm)
            mode = "llm"
        except pipeline.PrepUnavailable as exc:
            notice = f"AI 분석을 완료하지 못해 규칙 기반의 제한적인 정리로 대신했어요. ({exc.message})"
    if result is None:
        result = rules.analyze_rules(doc)
        if notice:
            result["meta"]["warnings"].insert(0, notice)
    prep_id = materials.save_result(meeting_id, result, mode)
    return present.scrub({"prepId": prep_id, "mode": mode, "result": result,
                          "share": materials.shares_view(prep_id)})


@router.get("/api/materials/{prep_id}")
def get_material(prep_id: str):
    r = materials.get_result(prep_id)
    if r is None:
        return _msg(404, "분석 결과를 찾을 수 없거나 보관 기간이 지났어요.")
    return present.scrub({**r, "share": materials.shares_view(prep_id)})


@router.post("/api/materials/{prep_id}/send")
def send_material(prep_id: str, payload: dict = Body(default={})):
    """주최자가 수신자 수를 확인하고 승인해야만 발송된다 (expectedCount 가 지금의 수신자 수와 같아야 한다)."""
    try:
        res = materials.send(prep_id, payload.get("expectedCount") if isinstance(payload, dict) else None)
    except materials.MaterialError as exc:
        return _msg(exc.status, exc.message)
    return {**res, "share": materials.shares_view(prep_id)}
