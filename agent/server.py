"""개인 에이전트 HTTP 서버.

핵심 로직은 agent.negotiator.LocalAgent 에 있고 이 파일은 얇은 껍데기다.
덕분에 fastapi 없이도 전체 협상을 테스트할 수 있다 (tests/, eval/demo_local.py).

실행 (PowerShell):
    $env:FAIRMEET_PROFILE = "profiles\\a.json"
    $env:FAIRMEET_TOKEN   = "tokens\\token_a.json"
    uvicorn agent.server:app --port 8001

실행 (bash):
    FAIRMEET_PROFILE=profiles/a.json FAIRMEET_TOKEN=tokens/token_a.json \
        uvicorn agent.server:app --port 8001

가짜 캘린더로 띄우려면 FAIRMEET_FAKE=1 을 추가한다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from agent.calendar_client import (CalendarUnavailable, EventNotFound, FakeCalendar,
                                   GoogleCalendar)
from agent.calendar_settings import FileSettings, MemorySettings, SettingsError
from agent.negotiator import LocalAgent
from agent.profile import Profile
from common import config

ROOT = Path(__file__).resolve().parent.parent

PROFILE_PATH = os.environ.get("FAIRMEET_PROFILE", "profiles/a.json")
TOKEN_PATH = os.environ.get("FAIRMEET_TOKEN", "")
GROUP_ID = os.environ.get("FAIRMEET_GROUP_ID", "demo-team")
USE_FAKE = os.environ.get("FAIRMEET_FAKE", "").lower() in ("1", "true", "yes")

app = FastAPI(title="FairMeet Personal Agent")

_profile = Profile.load(ROOT / PROFILE_PATH)
if TOKEN_PATH:
    _profile.token_path = TOKEN_PATH

if USE_FAKE:
    spec_path = ROOT / "profiles" / "fake_calendars.json"
    key = Path(PROFILE_PATH).stem
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    _backend = FakeCalendar(_profile.display_name, spec.get(key, {}).get("busy", {}),
                            events=spec.get(key, {}).get("events"))
    _settings = MemorySettings(spec.get(key, {}).get("colorSettings"))     # 시연용 가짜 설정
else:
    _backend = GoogleCalendar(_profile.token_path)
    # 사람마다 파일 하나: 설정은 이 에이전트(이 사람)의 것이고 다른 사람과 섞이지 않는다
    import hashlib
    _settings = FileSettings(ROOT / "settings" / (
        hashlib.sha256(_profile.user_key.encode("utf-8")).hexdigest()[:16] + ".json"))

_agent = LocalAgent(_profile, _backend, config.GROUP_SECRET, GROUP_ID, settings_store=_settings)


def _auth(authorization: str | None) -> None:
    """MVP 수준의 공유 Bearer 토큰 검증.

    운영이라면 에이전트별 키와 요청 서명이 필요하다. 해커톤 범위에서는
    '인증이 아예 없는 것'과 '있는 것'의 차이가 더 중요하다.
    """
    expected = f"Bearer {config.AGENT_TOKEN}"
    if authorization != expected:
        raise HTTPException(401, "에이전트 인증 실패")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "agent": _profile.display_name,
        "agent_id": _agent.agent_id,
        "backend": "fake" if USE_FAKE else "google",
        "group": GROUP_ID,
        # 중재자가 지원 여부를 미리 알 수 있게 한다 (없으면 옛 에이전트로 본다)
        "capabilities": sorted(_agent.CAPABILITIES),
    }


@app.get("/.well-known/agent-card.json")
def agent_card():
    """A2A 에서 영감을 받은 발견 엔드포인트.

    a2aCompliant=false 가 응답에 포함된다. A2A 1.0 규격의 Task 생명주기,
    보안 스킴, 버전 협상을 구현하지 않았으므로 호환을 주장하지 않는다.
    """
    return _agent.agent_card()


@app.post("/rpc/negotiation/open")
def open_negotiation(payload: dict = Body(...),
                     authorization: str | None = Header(None)):
    _auth(authorization)
    try:
        slots = [(dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b))
                 for a, b in payload["slots"]]
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, f"슬롯 형식 오류: {exc}")

    try:
        return _agent.open_negotiation(payload["sessionId"], slots, payload.get("options"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except CalendarUnavailable as exc:
        # 503 으로 구분해서 돌려준다. 중재자는 이걸 fail-closed 로 처리한다.
        return JSONResponse(status_code=503, content={"error": str(exc)})


@app.post("/rpc/negotiation/propose")
def propose(payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    try:
        return _agent.respond(
            payload["sessionId"], int(payload.get("round", 1)),
            int(payload["slotIndex"]), float(payload.get("balanceHint", 0.0)))
    except KeyError as exc:
        raise HTTPException(404, f"알 수 없는 세션: {exc}")
    except IndexError as exc:
        raise HTTPException(400, str(exc))


@app.post("/rpc/commit")
def commit(payload: dict = Body(...), authorization: str | None = Header(None)):
    """organizer 의 에이전트만 호출된다.

    중재자는 OAuth 토큰을 갖지 않는다 — 커밋은 토큰 소유자가 수행한다.
    """
    _auth(authorization)
    start = dt.datetime.fromisoformat(payload["start"])
    end = dt.datetime.fromisoformat(payload["end"])

    if payload.get("recheckOnly"):
        try:
            return {"conflict": _agent.recheck(start, end)}
        except CalendarUnavailable as exc:
            return JSONResponse(status_code=503, content={"error": str(exc)})

    event_id, created = _agent.commit(
        event_id=payload["eventId"], summary=payload["summary"],
        start=start, end=end,
        attendee_emails=payload.get("attendees", []),
        description=payload.get("description", ""))
    return {"eventId": event_id, "newlyCreated": created}


@app.post("/rpc/event/update")
def event_update(payload: dict = Body(...), authorization: str | None = Header(None)):
    """확정된 이벤트의 시각/참석자를 고친다 (일정 변경). 새 이벤트를 만들지 않는다."""
    _auth(authorization)
    try:
        start = (dt.datetime.fromisoformat(payload["start"])
                 if payload.get("start") else None)
        end = dt.datetime.fromisoformat(payload["end"]) if payload.get("end") else None
        if (start is None) != (end is None):
            raise ValueError("start 와 end 는 함께 주어야 합니다")
        attendees = payload.get("attendees")
        if attendees is not None and not isinstance(attendees, list):
            raise ValueError("attendees 는 목록이어야 합니다")
        event_id = str(payload["eventId"])
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, f"요청 형식 오류: {exc}")
    try:
        _agent.update_event(event_id, start, end, attendees)
        return {"updated": True}
    except EventNotFound as exc:
        return JSONResponse(status_code=404, content={"error": "event_not_found",
                                                      "detail": str(exc)})
    except CalendarUnavailable as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})


@app.post("/rpc/event/cancel")
def event_cancel(payload: dict = Body(...), authorization: str | None = Header(None)):
    """확정된 이벤트를 취소한다. 이미 취소됐으면 cancelled=false 로 성공 처리한다(멱등)."""
    _auth(authorization)
    try:
        event_id = str(payload["eventId"])
    except (KeyError, TypeError) as exc:
        raise HTTPException(400, f"요청 형식 오류: {exc}")
    try:
        return {"cancelled": _agent.cancel_event(event_id)}
    except CalendarUnavailable as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})

@app.get("/rpc/settings/colors")
def settings_colors_get(authorization: str | None = Header(None)):
    """일정 색상 설정 화면용: 팔레트, 현재 설정, 색상별 일정 개수. 일정의 제목·시각은 나가지 않는다."""
    _auth(authorization)
    try:
        return _agent.color_settings_view()
    except CalendarUnavailable as exc:
        return JSONResponse(status_code=503, content={"error": str(exc)})


@app.post("/rpc/settings/colors")
def settings_colors_save(payload: dict = Body(...), authorization: str | None = Header(None)):
    _auth(authorization)
    try:
        ev = payload.get("expectedVersion")
        if ev is not None and (not isinstance(ev, int) or isinstance(ev, bool)):
            raise SettingsError("설정 버전 형식이 올바르지 않습니다")
        return _agent.save_color_settings(payload.get("colors"), ev)
    except SettingsError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
