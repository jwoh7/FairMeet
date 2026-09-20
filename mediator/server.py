"""Mediator HTTP 서버.

실행:
    uvicorn mediator.server:app --port 8000 --reload

가짜 캘린더 모드(에이전트 서버 없이 한 프로세스에서 전부):
    FAIRMEET_FAKE=1 uvicorn mediator.server:app --port 8000
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from common import config
from common.ids import new_session_id
from common.slots import build_slots, format_slot, next_monday
from mediator import approval_flow, auth, change_flow, drafts, materials, nl_parse, product_api, security
from mediator import commit as commit_mod
from mediator import llm as llm_mod
from mediator import notifier as notifier_mod
from mediator import people as people_mod
from mediator import policy as policy_mod
from mediator import store
from mediator.request_model import MeetingRequest
from mediator.approval_web import router as approval_router
from mediator.change_web import router as change_router
from mediator.material_web import router as material_router
from mediator.negotiation import run_negotiation
from mediator.transport import HttpTransport, InProcessTransport

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("fairmeet.server")

ROOT = Path(__file__).resolve().parent.parent
USE_FAKE = os.environ.get("FAIRMEET_FAKE", "").lower() in ("1", "true", "yes")
GROUP_ID = os.environ.get("FAIRMEET_GROUP_ID", "demo-team")

AGENT_ENDPOINTS = json.loads(os.environ.get("FAIRMEET_AGENTS", json.dumps({
    "a": "http://127.0.0.1:8001",
    "b": "http://127.0.0.1:8002",
    "c": "http://127.0.0.1:8003",
})))

# /docs, /redoc 는 끈다 (경로 목록은 관리자만 /openapi.json 으로 볼 수 있다).
app = FastAPI(title="FairMeet Mediator", docs_url=None, redoc_url=None)
app.include_router(approval_router)
app.include_router(change_router)
app.include_router(material_router)
app.include_router(product_api.router)

# 본문이 지나치게 큰 요청은 파싱하기 전에 거절한다 (Content-Length 기준).
MAX_BODY_BYTES = 128 * 1024


@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH"):
        limit = MAX_BODY_BYTES
        for prefix, big in _BIG_BODY_PREFIXES.items():
            if request.url.path.startswith(prefix):
                limit = big
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > limit:
            return JSONResponse(status_code=413, content={"detail": "요청 본문이 너무 큽니다"})
    return await call_next(request)


# 파일 업로드처럼 큰 본문이 필요한 경로 (경로 접두사 -> 최대 바이트)
_BIG_BODY_PREFIXES: dict[str, int] = {}

# 관문(기본 거부)·보안 헤더·오류 정제. 위의 본문 크기 제한보다 바깥쪽에서 먼저 실행된다.
security.install(app)

# 회의 준비(PDF 분석)는 일정 협상과 독립된 선택 모듈이다. 꺼도(FAIRMEET_PREP_ENABLED=0) 일정 기능은
# 그대로이고, 이 모듈이 실패해도 협상·승인·캘린더 경로에는 영향이 없다.
if config.PREP_ENABLED:
    from prep.routes import router as prep_router
    app.include_router(prep_router)
    _BIG_BODY_PREFIXES["/prep/"] = config.PREP_MAX_BYTES + 256 * 1024
    _BIG_BODY_PREFIXES["/api/meetings/"] = config.PREP_MAX_BYTES + 256 * 1024

# SSE 구독자 (대시보드)
_subscribers: list[asyncio.Queue] = []
_recent_events: list[dict] = []
_transport = None
_agent_names: dict[str, str] = {}


def _broadcast(ev: dict) -> None:
    ev = dict(ev, ts=dt.datetime.now().isoformat(timespec="seconds"))
    _recent_events.append(ev)
    del _recent_events[:-200]
    for q in list(_subscribers):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass


def _build_transport():
    """가짜 모드면 한 프로세스 안에서, 아니면 HTTP 로 에이전트에 붙는다."""
    global _transport, _agent_names
    if _transport is not None:
        return _transport

    if USE_FAKE:
        from agent.calendar_client import FakeCalendar
        from agent.calendar_settings import MemorySettings
        from agent.negotiator import LocalAgent
        from agent.profile import Profile

        spec = json.loads(
            (ROOT / "profiles" / "fake_calendars.json").read_text(encoding="utf-8"))
        agents = {}
        for key in ("a", "b", "c"):
            prof = Profile.load(ROOT / "profiles" / f"{key}.json")
            backend = FakeCalendar(prof.display_name, spec[key]["busy"], events=spec[key].get("events"))
            ag = LocalAgent(prof, backend, config.GROUP_SECRET, GROUP_ID,
                            settings_store=MemorySettings(spec[key].get("colorSettings")))
            agents[ag.agent_id] = ag
            _agent_names[ag.agent_id] = prof.display_name
        _transport = InProcessTransport(agents)
    else:
        import httpx
        from common.ids import group_pseudonym
        from agent.profile import Profile

        endpoints = {}
        for key, url in AGENT_ENDPOINTS.items():
            prof = Profile.load(ROOT / "profiles" / f"{key}.json")
            aid = group_pseudonym(config.GROUP_SECRET, GROUP_ID, prof.user_key)
            endpoints[aid] = url
            _agent_names[aid] = prof.display_name
        _transport = HttpTransport(endpoints, config.AGENT_TOKEN)
    return _transport


@app.on_event("startup")
def _startup():
    report = store.init_db()
    if report["backup"]:
        log.info("DB 를 v%s → v%s 로 마이그레이션했습니다. 백업: %s",
                 report["from"], report["to"], report["backup"])
    settled = approval_flow.settle_expired_sessions()
    if settled["expired"] or settled["finalize"]:
        log.info("응답 기한이 지난 세션 정리 — 종료 %d건, 기준 충족으로 확정 진행 %d건",
                 len(settled["expired"]), len(settled["finalize"]))
    stale_changes = change_flow.expire_stale_changes()
    if stale_changes:
        log.info("투표 시간이 지난 일정 변경 요청 %d건을 만료시켰습니다", len(stale_changes))
    change_flow.expire_stale_intakes()
    _build_transport()
    change_flow.reconcile_all()
    _run_in_background(change_flow.resume_stuck_applications, _build_transport())
    log.info("Mediator 기동 — 모드=%s 그룹=%s 이메일=%s 최대승인라운드=%d",
             "fake" if USE_FAKE else "http", GROUP_ID, config.EMAIL_BACKEND,
             config.MAX_APPROVAL_ROUNDS)
    try:
        security.purge_audit()
        materials.purge_expired()
    except Exception:                                            # noqa: BLE001
        log.exception("오래된 보안 기록/자료 정리 실패")
    if config.using_dev_secrets():
        log.warning("개발용 기본 비밀키를 사용 중입니다. .env 를 설정하세요.")
    if auth.is_configured():
        log.info("관리자 로그인: 설정됨 (세션 %d분, 무동작 %d분)", config.ADMIN_SESSION_MIN,
                 config.ADMIN_IDLE_MIN)
    else:
        log.warning("관리자 자격정보가 설정되지 않았습니다: 이 컴퓨터에서 직접 접속한 요청만 관리자 기능을 쓸 수 "
                    "있습니다 (터널로 공개하기 전에 python -m scripts.set_admin_password 를 실행하세요).")
    for w in notifier_mod.base_url_warnings():
        log.warning("이메일 설정: %s", w)


@app.get("/health")
def health(request: Request):
    # 공개 주소로 들어온(인증되지 않은) 요청에는 상태만 알려 준다. 이 컴퓨터에서 직접 확인하면 전부 보인다.
    if auth.authenticate(request) is None and not auth.is_direct_local(request):
        return {"status": "ok"}
    return {"status": "ok", "mode": "fake" if USE_FAKE else "http",
            "group": GROUP_ID, "agents": len(_agent_names),
            "dev_secrets": config.using_dev_secrets(),
            "email_backend": config.EMAIL_BACKEND}


def _participants(group_id: str) -> list[dict]:
    """세션 참가자 목록. name 은 조건 해석이 이름을 대조하는 데 쓴다."""
    from agent.profile import Profile
    from common.ids import group_pseudonym

    parts = []
    for key in ("a", "b", "c"):
        prof = Profile.load(ROOT / "profiles" / f"{key}.json")
        aid = group_pseudonym(config.GROUP_SECRET, group_id, prof.user_key)
        parts.append({"agent_id": aid, "display_name": prof.display_name,
                      "name": prof.display_name, "email": prof.email,
                      "is_organizer": prof.is_organizer})
    return parts


_cap_cache: dict = {}


def _capabilities(parts: list[dict]) -> dict:
    """참가자 에이전트가 지원하는 선택 기능 (잠깐 캐시). 응답이 없으면 지원하지 않는 것으로 본다."""
    import time
    key = tuple(sorted(p["agent_id"] for p in parts))
    hit = _cap_cache.get(key)
    if hit and time.time() - hit[0] < 10:
        return hit[1]
    transport = _build_transport()
    caps = {}
    for p in parts:
        try:
            c = transport.capabilities(p["agent_id"])
        except Exception:                                        # noqa: BLE001
            c = None
        caps[p["agent_id"]] = set(c or ())
    _cap_cache[key] = (time.time(), caps)
    return caps


def _draft_env(group_id: str) -> drafts.DraftEnv:
    parts = _participants(group_id)
    return drafts.DraftEnv(participants=parts, capabilities=_capabilities(parts),
                           llm=llm_mod.get_llm())


def _policy_from_payload(spec, group_id: str, chosen=None):
    """수동 협상의 승인 방식 지정. 잘못되면 400 (사용자용 문장)."""
    if not spec:
        return None
    if not isinstance(spec, dict):
        raise HTTPException(400, "approvalPolicy 형식이 올바르지 않습니다")
    parts = chosen if chosen is not None else _participants(group_id)
    by_name = {p["name"]: p["agent_id"] for p in parts}
    req_names = spec.get("required") or []
    if not isinstance(req_names, list) or any(n not in by_name for n in req_names):
        raise HTTPException(400, "필수 참석자 이름이 참가자 목록에 없습니다")
    organizer = next((p["agent_id"] for p in parts if p["is_organizer"]), None)
    try:
        return policy_mod.build_policy(
            spec.get("mode", "all"), min_count=spec.get("minCount"),
            min_ratio=spec.get("minRatio"), required=[by_name[n] for n in req_names],
            participant_ids=[p["agent_id"] for p in parts], organizer_id=organizer)
    except policy_mod.PolicyError as exc:
        raise HTTPException(400, str(exc)) from None


def _inherit_conditions(session_id: str, mode: str, duration: int, days: int) -> dict:
    """날짜 범위 넓히기·시간 줄이기 같은 새 협상이 원래 협상의 승인 방식·회의 조건을 이어받게 한다.

    조건은 새 슬롯 그리드에 맞춰 다시 평가한다. 조건이 없던 옛 세션은 그대로 {} (기존 동작).
    """
    parent = store.get_session(session_id)
    if parent is None:
        return {}
    # 참가자는 원래 협상에서 확정한 사람 그대로 이어받는다 (몰래 바꾸지 않는다)
    snapshot = [{"agent_id": p["agent_id"], "display_name": p["display_name"], "name": p["display_name"],
                 "email": p["email"], "is_organizer": bool(p["is_organizer"])}
                for p in store.get_participants(session_id)]
    pol = policy_mod.ApprovalPolicy.from_row(parent)
    if not parent["constraints_json"]:
        out = {"participants": snapshot}
        if pol.mode != "all":
            out["policy"] = pol
        return out

    req = MeetingRequest.from_dict(json.loads(parent["constraints_json"]))
    first = store.get_slots(session_id)
    today = dt.datetime.now(dt.timezone.utc).astimezone().date()
    start = first[0][0].date() if first and first[0][0].date() >= today else next_monday()
    req.duration_min = duration
    req.date_start, req.date_end = start, start + dt.timedelta(days=days - 1)
    req.issues = [i for i in req.issues if i.origin == "parse"]
    ev, _ = drafts.refresh(req, _draft_env(parent["group_id"]))
    return {"policy": ev.policy, "constraints": req.to_dict(),
            "slot_effects": ev.slot_effects(), "start_date": start,
            "parent_session_id": session_id, "participants": snapshot}


def _start_negotiation(mode: str, duration: int, group_id: str, days: int = 7, *,
                       policy=None, constraints=None, slot_effects=None,
                       start_date=None, parent_session_id=None,
                       session_id: str | None = None, participants=None) -> dict:
    """새 협상 세션을 만들고 1순위 제안까지 진행한 뒤, 승인 요청 이메일을 보낸다.

    응답에는 승인 링크가 없다. 링크는 각 참가자의 이메일로만 나간다.
    """
    if mode not in ("work", "social"):
        raise HTTPException(400, "mode 는 work 또는 social 이어야 합니다")
    if not 1 <= days <= approval_flow.MAX_SEARCH_DAYS:
        raise HTTPException(400, f"days 는 1~{approval_flow.MAX_SEARCH_DAYS} 이어야 합니다")
    if duration < approval_flow.MIN_DURATION_MIN:
        raise HTTPException(400, f"회의 시간은 최소 {approval_flow.MIN_DURATION_MIN}분입니다")

    transport = _build_transport()
    slots = build_slots(start_date or next_monday(), mode, duration, days=days)
    session_id = session_id or new_session_id()
    # 참가자는 호출자가 확정한 목록만 쓴다 (없으면 등록된 전체). 몰래 더하거나 빼지 않는다.
    parts = [dict(p) for p in participants] if participants is not None else _participants(group_id)
    if len(parts) < 2:
        raise HTTPException(400, "참가자는 두 명 이상이어야 합니다")

    # 승인 정책·회의 조건은 여기서 세션에 한 번 저장되고 이후 바뀌지 않는다.
    store.create_session(session_id, group_id, mode, duration, slots, parts,
                         policy=policy, constraints=constraints, slot_effects=slot_effects,
                         parent_session_id=parent_session_id)

    lam = config.LAMBDA_WORK if mode == "work" else config.LAMBDA_SOCIAL
    outcome = run_negotiation(session_id, transport, slots, group_id,
                              lam=lam, emit=_broadcast)

    if outcome.state != "PENDING_HUMAN_APPROVAL":
        return {"sessionId": session_id, "state": outcome.state,
                "reason": outcome.reason,
                "outcome": approval_flow.outcome_for(outcome.state)}

    # 후보가 정해졌으면 참가자별 개인 링크를 이메일로 보낸다. 발송이 실패해도
    # 협상을 성공처럼 보이게 하지 않는다 — 실패는 응답과 대시보드에 그대로 드러나고
    # 세션은 승인 대기 상태로 남아 재시도할 수 있다.
    summary = approval_flow.dispatch_notifications(session_id)
    view = approval_flow.session_view(session_id)
    s, e = slots[outcome.slot_index]
    return {
        "sessionId": session_id,
        "state": outcome.state,
        "slot": format_slot(s, e),
        "rounds": outcome.rounds,
        "policy": view["policy"],
        "notification": view["notification"],
        "emailFailures": [{"name": n, "error": err} for n, err in summary.errors],
        "needsAttention": view["notification"]["status"] in
                          ("partial_failure", "all_failed"),
    }


@app.post("/negotiate")
def negotiate(payload: dict = Body(default={})):
    """협상을 시작한다. 승인 요청은 참가자 이메일로 발송된다."""
    try:
        duration = int(payload.get("durationMin", 60))
        days = int(payload.get("days", 7))
    except (TypeError, ValueError):
        raise HTTPException(400, "durationMin, days 는 숫자여야 합니다") from None
    group_id = payload.get("groupId", GROUP_ID)
    chosen = None
    if payload.get("participants") is not None:
        names = payload["participants"]
        all_parts = _participants(group_id)
        by_name = {p["name"]: p for p in all_parts}
        if (not isinstance(names, list) or any(not isinstance(n, str) or n not in by_name for n in names)
                or len(set(names)) != len(names)):
            raise HTTPException(400, "participants 는 등록된 참가자 이름 목록이어야 합니다")
        chosen = people_mod.select(all_parts, [by_name[n]["agent_id"] for n in names])
    policy = _policy_from_payload(payload.get("approvalPolicy"), group_id, chosen)
    return _start_negotiation(payload.get("mode", "work"), duration, group_id, days,
                              policy=policy, participants=chosen)


def _draft_error(exc: drafts.DraftError) -> JSONResponse:
    body: dict = {"detail": exc.message}
    if exc.reasons:
        body["reasons"] = exc.reasons
    return JSONResponse(status_code=exc.status, content=body)


@app.post("/meeting-requests")
def create_meeting_request(payload: dict = Body(...)):
    """자연어 회의 요청을 해석해 초안을 만든다. 협상은 시작하지 않는다.

    응답의 draft 를 사용자에게 보여 주고, 확인 질문에 답하고, 자동 처리할 수 없는 조건을
    수동 확인/제거한 뒤 /confirm 을 눌러야 협상이 시작된다.
    """
    group_id = payload.get("groupId", GROUP_ID)
    try:
        return drafts.create_draft(payload.get("text"), _draft_env(group_id), group_id)
    except nl_parse.InvalidText as exc:
        raise HTTPException(400, str(exc)) from None


@app.get("/meeting-requests/{draft_id}")
def get_meeting_request(draft_id: str):
    try:
        return drafts.get_view(draft_id, _draft_env(drafts.group_of(draft_id)))
    except drafts.DraftError as exc:
        return _draft_error(exc)


@app.post("/meeting-requests/{draft_id}/resolve")
def resolve_meeting_request(draft_id: str, payload: dict = Body(default={})):
    """확인 질문 답 / 조건 제거 / 수동 확인 / 강도·우선순위·소요 시간·기간 수정."""
    try:
        return drafts.resolve_draft(draft_id, payload,
                                    _draft_env(drafts.group_of(draft_id)))
    except drafts.DraftError as exc:
        return _draft_error(exc)


@app.post("/meeting-requests/{draft_id}/cancel")
def cancel_meeting_request(draft_id: str):
    try:
        return {"cancelled": drafts.cancel_draft(draft_id)}
    except drafts.DraftError as exc:
        return _draft_error(exc)


def _confirm_and_start(draft_id: str) -> dict:
    """저장된 초안을 다시 검증하고 (통과해야만) 협상을 시작한다.

    Raises: drafts.DraftError — 검증 실패, 이미 시작됨(더블 클릭·동시 요청), 만료.
    """
    group_id = drafts.group_of(draft_id)
    plan = drafts.confirm_plan(draft_id, _draft_env(group_id))
    session_id = new_session_id()
    if not drafts.claim_draft(draft_id, session_id):        # 더블 클릭/동시 요청 방어
        raise drafts.DraftError(409, "이미 협상이 시작되었거나 만료된 초안입니다")
    req = plan.request
    try:
        result = _start_negotiation(
            req.mode, req.duration_min, group_id, req.days, policy=plan.policy,
            constraints=req.to_dict(), slot_effects=plan.effects,
            start_date=req.date_start, session_id=session_id, participants=plan.participants)
    except BaseException:
        if store.get_session(session_id) is None:            # 세션이 없을 때만 다시 열어 준다
            drafts.release_draft(draft_id)
        raise
    result["draftId"] = draft_id
    return result


@app.post("/meeting-requests/{draft_id}/confirm")
def confirm_meeting_request(draft_id: str):
    """'이 조건으로 협상 시작'. 저장된 초안을 다시 검증해 통과해야만 협상이 시작된다."""
    try:
        return _confirm_and_start(draft_id)
    except drafts.DraftError as exc:
        return _draft_error(exc)


def _agent_name_map() -> dict[str, str]:
    _build_transport()
    return dict(_agent_names)


product_api.configure(group_id=GROUP_ID, participants=_participants, draft_env=_draft_env,
                      confirm_and_start=_confirm_and_start, agent_names=_agent_name_map,
                      spawn=lambda fn: _spawn(fn))


_bg_threads: list[threading.Thread] = []


def _spawn(fn) -> None:
    """서버가 시작한 백그라운드 작업. 테스트가 끝날 때 join_background() 로 기다릴 수 있다."""
    th = threading.Thread(target=fn, daemon=True)
    _bg_threads[:] = [x for x in _bg_threads if x.is_alive()]
    _bg_threads.append(th)
    th.start()


def join_background(timeout: float = 15.0) -> None:
    """진행 중인 백그라운드 작업(확정, 메일 발송 등)이 끝나기를 기다린다 (테스트용)."""
    import time
    end = time.time() + timeout
    for th in list(_bg_threads):
        th.join(max(0.0, end - time.time()))


def schedule_finalize(session_id: str) -> None:
    """전원 승인 직후 백그라운드로 재확인 + 커밋을 수행한다."""
    def _run():
        try:
            res = commit_mod.finalize(session_id, _build_transport(),
                                      emit=_broadcast)
            log.info("finalize %s → %s", session_id[:8], res.state)
            if res.state == "COMMITTED":
                # 확정 안내 메일(변경 요청 링크 포함). 실패해도 확정에는 영향이 없다.
                if change_flow.prepare_confirmations(session_id):
                    change_flow.dispatch_mail(session_id, "", ("confirm",))
                for rid in _requests_awaiting_result(session_id):
                    change_flow.dispatch_mail(*rid, ("result",))
            if res.state == "STALE_CONFLICT":
                # 이벤트는 만들어지지 않았다. 충돌 슬롯을 제외하고 다음 후보로 새 승인 라운드.
                rec = approval_flow.recover_from_stale_conflict(session_id)
                log.info("stale 복구 %s → %s", session_id[:8], rec.status)
                if rec.status == "reopened":
                    approval_flow.dispatch_notifications(session_id)
        except Exception:
            log.exception("finalize 실패")
    _spawn(_run)


def schedule_dispatch(session_id: str) -> None:
    """새 제안이 열린 직후 백그라운드로 승인 요청 이메일을 보낸다."""
    def _run():
        try:
            approval_flow.dispatch_notifications(session_id)
        except Exception:
            log.exception("승인 요청 발송 실패")
    _spawn(_run)


approval_flow.configure(emit=_broadcast, on_all_approved=schedule_finalize,
                        on_new_round=schedule_dispatch)


# ---- 일정 변경 (확정된 회의) --------------------------------------------------------------------

def _requests_awaiting_result(session_id: str) -> list[tuple[str, str]]:
    """이 세션(재협상 세션이면 그 원래 세션 포함)에 딸린, 결과 메일이 남은 변경 요청."""
    row = store.get_session(session_id)
    if row is None:
        return []
    sids = [session_id] + ([row["parent_session_id"]] if row["change_request_id"] else [])
    out = []
    with store.connect() as c:
        for sid in sids:
            for r in c.execute(
                    "SELECT DISTINCT request_id FROM change_mail WHERE session_id = ? "
                    "AND kind = 'result' AND status IN ('pending', 'failed')", (sid,)):
                out.append((sid, r["request_id"]))
    return out


def _run_in_background(fn, *args) -> None:
    def _run():
        try:
            fn(*args)
        except Exception:                                        # noqa: BLE001
            log.exception("변경 요청 백그라운드 작업 실패")
    _spawn(_run)


def _request_session(request_id: str) -> str | None:
    with store.connect() as c:
        r = c.execute("SELECT session_id FROM change_requests WHERE request_id = ?",
                      (request_id,)).fetchone()
    return r["session_id"] if r else None


def _change_created(request_id: str) -> None:
    sid = _request_session(request_id)
    if sid:
        _run_in_background(change_flow.dispatch_mail, sid, request_id, ("vote",))


def _change_settled(request_id: str) -> None:
    sid = _request_session(request_id)
    if sid:
        _run_in_background(change_flow.dispatch_mail, sid, request_id, ("result",))


def _change_accepted(request_id: str) -> None:
    def _apply():
        state = change_flow.apply_request(request_id, _build_transport())
        if state in ("applied", "failed"):
            _change_settled(request_id)
    _run_in_background(_apply)


change_flow.configure(emit=_broadcast, on_created=_change_created,
                      on_accepted=_change_accepted, on_settled=_change_settled)


@app.get("/session/{session_id}")
def session_state(session_id: str):
    row = store.get_session(session_id)
    if row is None:
        raise HTTPException(404, "세션 없음")
    ok, no, total = store.approval_tally(session_id)
    return {"sessionId": session_id, "state": row["state"],
            "round": row["round"], "approvals": {"yes": ok, "no": no, "total": total},
            "eventId": row["google_event_id"], "reason": row["fail_reason"]}


@app.get("/sessions/latest")
def latest_session():
    with store.connect() as c:
        row = c.execute("SELECT session_id FROM sessions "
                        "ORDER BY created_at DESC LIMIT 1").fetchone()
    return {"sessionId": row["session_id"] if row else None}


@app.get("/session/{session_id}/dashboard")
def session_dashboard(session_id: str):
    """관리자 대시보드용 요약. 승인 링크·토큰·개인별 regret·거절한 사람은 없다."""
    approval_flow.settle_expired_sessions()      # 기한이 지난 세션은 정책을 다시 검증해 한 번만 결정한다
    change_flow.expire_stale_changes()
    change_flow.expire_stale_intakes()
    change_flow.reconcile_all()
    view = approval_flow.session_view(session_id)
    if view is None:
        raise HTTPException(404, "세션 없음")
    view["change"] = change_flow.change_view(session_id)
    return view


@app.post("/session/{session_id}/change-links/send")
def send_change_links(session_id: str):
    """확정된 회의의 참석자에게 '확정 안내(일정 변경 요청 링크)' 메일을 보낸다 (재시도 겸용).

    이미 보낸 사람에게는 다시 보내지 않는다. 링크는 이메일로만 나가고 어떤 응답에도 없다.
    """
    row = store.get_session(session_id)
    if row is None:
        raise HTTPException(404, "세션 없음")
    if row["state"] != "COMMITTED":
        raise HTTPException(409, "확정된 회의가 아닙니다. 메일을 보내지 않았습니다.")
    change_flow.prepare_confirmations(session_id)
    summary = change_flow.dispatch_mail(session_id, "", ("confirm",))
    return {"sent": summary.sent, "failed": summary.failed,
            "errors": [{"name": n, "error": e} for n, e in summary.errors]}


@app.post("/session/{session_id}/notifications/retry")
def retry_notifications(session_id: str):
    """발송에 실패했거나 대기 중인 참가자에게만 승인 요청을 다시 보낸다.

    이미 발송된 참가자에게는 다시 보내지 않는다. 다시 보낼 때는 새 링크가 발급되고
    같은 참가자의 이전 미사용 링크는 무효가 된다.
    """
    row = store.get_session(session_id)
    if row is None:
        raise HTTPException(404, "세션 없음")
    if row["state"] != "PENDING_HUMAN_APPROVAL":
        raise HTTPException(409, "승인 대기 중인 협상이 아닙니다. 이메일을 보내지 않았습니다.")
    summary = approval_flow.dispatch_notifications(session_id)
    return {"sent": summary.sent, "failed": summary.failed,
            "errors": [{"name": n, "error": e} for n, e in summary.errors]}


@app.post("/session/{session_id}/followup")
def followup(session_id: str, payload: dict = Body(default={})):
    """종료된 협상 뒤의 선택: 범위 넓히기 / 회의 시간 줄이기 / 이번 협상 종료."""
    action = payload.get("action")
    if action == "close":
        if not approval_flow.close_session(session_id):
            raise HTTPException(409, "종료된 협상이 아닙니다")
        return {"closed": True}
    try:
        plan = approval_flow.followup_plan(session_id, action)
    except LookupError:
        raise HTTPException(404, "세션 없음") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    inherit = _inherit_conditions(session_id, plan["mode"], plan["durationMin"],
                                  plan["days"])
    return _start_negotiation(plan["mode"], plan["durationMin"],
                              plan["groupId"], plan["days"], **inherit)


@app.get("/ledger")
def ledger(request: Request, group: str = ""):
    """양보 잔액 원장. 시연 모드에서만 볼 수 있다 (서버가 다시 확인한다).

    개인별 regret 은 노출하지 않고 잔액만 보여준다.
    """
    product_api.require_demo(request)
    gid = group or GROUP_ID
    _build_transport()
    bal = store.get_balance(gid, list(_agent_names))
    return {"group": gid, "entries": [
        {"agentId": aid, "name": _agent_names.get(aid, aid[:12]),
         "balance": round(v, 2)}
        for aid, v in sorted(bal.items(), key=lambda kv: -kv[1])]}


@app.get("/events")
async def events(request: Request):
    """SSE 스트림(원시 협상 로그). 시연 모드에서만 열린다. 제품 화면은 이것 없이도 폴링으로 동작한다
    (Cloudflare Quick Tunnel 은 SSE 를 지원하지 않는다)."""
    product_api.require_demo(request)
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.append(q)

    async def gen():
        try:
            for ev in _recent_events[-30:]:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            while True:
                ev = await q.get()
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            if q in _subscribers:
                _subscribers.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """화면 뼈대. 데이터가 없으므로 공개이고, 실제 데이터는 로그인한 관리자만 /api 로 받는다."""
    path = ROOT / "dashboard" / "index.html"
    if not path.exists():
        return HTMLResponse("<p>dashboard/index.html 이 없습니다</p>")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


# 정적 자산은 허용 목록에 있는 파일만 내준다 (경로 조작 불가).
_ASSETS = {"fairmeet.css": "text/css; charset=utf-8", "app.js": "application/javascript; charset=utf-8"}


@app.get("/assets/{name}")
def asset(name: str):
    ctype = _ASSETS.get(name)
    path = ROOT / "dashboard" / name
    if ctype is None or not path.is_file():
        raise HTTPException(404, "없는 파일")
    return Response(path.read_bytes(), media_type=ctype, headers={"Cache-Control": "no-cache"})


# Slack 은 선택 사항. 설정되어 있을 때만 라우트를 붙인다.
try:
    if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_SIGNING_SECRET"):
        from fastapi import Request
        from slackbot.handler import slack_handler

        @app.post("/slack/events")
        async def slack_events(req: Request):
            return await slack_handler.handle(req)

        log.info("Slack 라우트 활성화")
except Exception as exc:      # noqa: BLE001
    log.info("Slack 비활성 (%s)", exc)
