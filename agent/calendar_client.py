"""캘린더 백엔드 추상화.

두 구현을 같은 인터페이스 뒤에 둔다:
  FakeCalendar   — 오프라인. OAuth/네트워크 없이 전체 흐름을 시연할 수 있다.
  GoogleCalendar — 실제 Google Calendar API.

발표 당일 Wi-Fi가 죽거나 토큰이 만료되어도 FakeCalendar 로 즉시 전환해
시연을 마칠 수 있다. 이것이 해커톤에서 가장 값싼 보험이다.

조회 범위 (개인정보 최소화)
  일정 종류(고정/유동/무시)를 나누려면 색상·한가함 표시를 읽어야 하므로 events().list 를 쓴다. 단
  Google 의 `fields` 부분 응답으로 아래 항목만 요청한다:
      start, end, colorId, transparency, eventType, status
  제목(summary), 설명, 장소, 참석자, 화상회의 정보, 첨부, 만든 사람은 요청하지 않으므로 이 프로세스에 도착하지도
  않는다. 이벤트 ID 도 요청하지 않는다. 이 파일은 events().get / instances / watch 를 호출하지 않는다.
  tests/test_no_event_read.py 가 이를 정적으로 검사한다.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Google Calendar 커스텀 event ID 규칙:
#   base32hex — 소문자 a-v 와 숫자 0-9, 길이 5~1024
_EVENT_ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")


class CalendarUnavailable(RuntimeError):
    """조회 실패. 절대 '한가함'으로 해석하지 말 것 (fail-closed).

    실패를 빈 시간으로 처리하면 사람이 실제로 바쁜 시간에 회의가 잡힌다.
    이 예외는 반드시 상위로 전파되어 인간에게 넘어가야 한다.
    """


class EventNotFound(RuntimeError):
    """수정/취소하려는 이벤트가 이 캘린더에 없다. 다른 이벤트를 새로 만들어 대신하지 않는다."""


@dataclass(frozen=True)
class CalEvent:
    """분류에 필요한 최소 항목만 가진 일정. 제목·설명·장소·참석자는 이 객체에 자리조차 없다."""
    start: dt.datetime
    end: dt.datetime
    color_id: str | None = None          # Google 일정 색상 번호 ("1"~"11"), 지정 안 했으면 None
    transparency: str = "opaque"         # opaque(바쁨) | transparent(한가함)
    event_type: str = "default"          # default | outOfOffice | focusTime | workingLocation ...
    status: str = "confirmed"            # confirmed | tentative | cancelled


class CalendarBackend(Protocol):
    def fetch_events(self, time_min: dt.datetime, time_max: dt.datetime) -> list[CalEvent]:
        """기간과 겹치는 일정(최소 항목만). 조회 실패는 CalendarUnavailable — 빈 목록으로 대신하지 않는다."""
        ...

    def commit_event(self, *, event_id: str, summary: str,
                     start: dt.datetime, end: dt.datetime,
                     attendee_emails: list[str], description: str = ""
                     ) -> tuple[str, bool]:
        ...

    def update_event(self, *, event_id: str, start: dt.datetime | None = None,
                     end: dt.datetime | None = None,
                     attendee_emails: list[str] | None = None) -> bool:
        """기존 이벤트의 시각/참석자를 고친다. 새 이벤트를 만들지 않는다.
        이벤트가 없으면 EventNotFound."""
        ...

    def cancel_event(self, event_id: str) -> bool:
        """이벤트를 취소한다. 이미 없으면 False (멱등)."""
        ...


def make_event_id(session_id: str) -> str:
    """협상 세션 ID로부터 결정론적 event ID를 만든다.

    같은 ID로 다시 insert 하면 Google 이 409 를 반환하므로, 이것이
    멱등성 키가 된다. 네트워크 재시도나 중복 클릭에도 이벤트는 하나만 생긴다.
    """
    eid = "fm" + session_id.replace("-", "").lower()
    if not _EVENT_ID_RE.fullmatch(eid):
        raise ValueError(f"잘못된 event ID: {eid!r} (base32hex a-v/0-9, 5~1024자)")
    return eid


def _overlap(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


# --------------------------------------------------------------------------
# 오프라인 백엔드
# --------------------------------------------------------------------------

class FakeCalendar:
    """JSON 파일에 담긴 가짜 일정으로 동작하는 백엔드.

    실제 Google 과 같은 인터페이스를 제공하므로, 협상 로직은 어느 쪽이
    붙어 있는지 알 필요가 없다.
    """

    # 옛 형식(캘린더 ID → 구간)의 캘린더 ID 를 새 형식의 색상 번호로 옮기는 표 (테스트·시연 데이터용)
    LEGACY_COLORS = {"flexible": "5", "flex": "5"}

    def __init__(self, owner: str, busy_spec: dict | None = None,
                 store_path: str | Path | None = None,
                 fail_fetch: bool = False, events: list | None = None):
        """busy_spec 형식 (옛 형식도 받는다):
            {"primary": [["2026-09-21T10:00:00+09:00", "2026-09-21T11:00:00+09:00"], ...],
             "flexible": [...]}
        events: 새 형식. [{"start", "end", "colorId"?, "transparency"?, "eventType"?, "status"?}]
        """
        self.owner = owner
        self.fail_fetch = fail_fetch          # 장애 주입 테스트용
        self.fail_update = False              # 이벤트 수정/취소 실패 주입 (테스트용)
        self.update_calls = 0
        self.fetch_calls = 0
        self._events: list[CalEvent] = []
        self.created_events: dict[str, dict] = {}
        self.cancelled_events: dict[str, dict] = {}
        self.store_path = Path(store_path) if store_path else None

        for cal, blocks in (busy_spec or {}).items():
            for a, b in blocks:
                self._events.append(CalEvent(
                    dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b),
                    color_id=self.LEGACY_COLORS.get(cal)))
        for e in events or []:
            self._events.append(CalEvent(
                dt.datetime.fromisoformat(e["start"]), dt.datetime.fromisoformat(e["end"]),
                color_id=e.get("colorId"), transparency=e.get("transparency", "opaque"),
                event_type=e.get("eventType", "default"), status=e.get("status", "confirmed")))

    @staticmethod
    def from_file(owner: str, path: str | Path) -> "FakeCalendar":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return FakeCalendar(owner, data.get("busy", {}), store_path=path, events=data.get("events"))

    def add_busy(self, calendar_id: str, start: dt.datetime, end: dt.datetime,
                 color_id: str | None = None, transparency: str = "opaque",
                 event_type: str = "default", status: str = "confirmed") -> None:
        """시연 중 '일정이 새로 생겼다'를 흉내내기 위한 도우미.

        승인 대기 중에 호출하면 STALE_CONFLICT 경로를 실연할 수 있다.
        """
        self._events.append(CalEvent(
            start, end, color_id=color_id or self.LEGACY_COLORS.get(calendar_id),
            transparency=transparency, event_type=event_type, status=status))

    def fetch_events(self, time_min, time_max):
        self.fetch_calls += 1
        if self.fail_fetch:
            raise CalendarUnavailable(
                f"[FakeCalendar] {self.owner} 조회 실패를 의도적으로 주입함")
        return [ev for ev in self._events if _overlap(time_min, time_max, ev.start, ev.end)]

    def commit_event(self, *, event_id, summary, start, end,
                     attendee_emails, description=""):
        if event_id in self.created_events:
            return event_id, False          # 이미 존재 = 멱등
        self.created_events[event_id] = {
            "id": event_id, "summary": summary,
            "start": start.isoformat(), "end": end.isoformat(),
            "attendees": list(attendee_emails), "description": description,
            "meetLink": f"https://meet.example/{event_id[:12]}",
        }
        # 생성된 이벤트는 그 사람의 캘린더를 실제로 점유한다 (색상 없음, 바쁨)
        self._events.append(CalEvent(start, end))
        return event_id, True

    def _find_busy(self, start_iso: str, end_iso: str):
        s, e = dt.datetime.fromisoformat(start_iso), dt.datetime.fromisoformat(end_iso)
        for i, ev in enumerate(self._events):
            if ev.start == s and ev.end == e and ev.color_id is None:
                return i
        return None

    def update_event(self, *, event_id, start=None, end=None, attendee_emails=None):
        ev = self.created_events.get(event_id)
        if ev is None:
            raise EventNotFound(f"[FakeCalendar] {self.owner} 캘린더에 이벤트 {event_id} 가 없습니다")
        if self.fail_update:
            raise CalendarUnavailable(f"[FakeCalendar] {self.owner} 이벤트 수정 실패를 주입함")
        if start is not None and end is not None:
            i = self._find_busy(ev["start"], ev["end"])
            if i is not None:
                self._events[i] = CalEvent(start, end)
            else:
                self._events.append(CalEvent(start, end))
            ev["start"], ev["end"] = start.isoformat(), end.isoformat()
        if attendee_emails is not None:
            ev["attendees"] = list(attendee_emails)
        self.update_calls += 1
        return True

    def cancel_event(self, event_id):
        ev = self.created_events.get(event_id)
        if ev is None:
            return False                                # 이미 취소됨 = 멱등
        if self.fail_update:
            raise CalendarUnavailable(f"[FakeCalendar] {self.owner} 이벤트 취소 실패를 주입함")
        i = self._find_busy(ev["start"], ev["end"])
        if i is not None:
            del self._events[i]
        self.cancelled_events[event_id] = self.created_events.pop(event_id)
        return True

# --------------------------------------------------------------------------
# 실제 Google 백엔드
# --------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.events.owned",
]

CREDENTIALS_FILE = "credentials.json"

# events.list 에서 읽는 항목. 제목·설명·장소·참석자 등은 여기에 넣지 않는다 (tests/test_no_event_read.py 가 검사).
EVENT_FIELDS = "nextPageToken,items(start,end,colorId,transparency,eventType,status)"
EVENTS_PAGE_SIZE = 250
EVENTS_MAX_PAGES = 12

# FreeBusy 조회 재시도 횟수. 오래 놀던 에이전트의 첫 호출이 끊긴 연결 때문에 실패하는 것을 막는다.
FREEBUSY_RETRIES = 2


def get_google_service(token_path: str):
    """사용자별 토큰으로 Calendar 서비스를 만든다.

    테스트 모드 외부 앱의 refresh token 은 7일 후 만료되므로,
    시연 전날 반드시 scripts/bootstrap_tokens.py 를 다시 실행해야 한다.
    """
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise CalendarUnavailable(
            "Google 클라이언트 라이브러리가 없습니다. "
            "pip install -r requirements.txt 를 실행하거나, "
            "오프라인이면 FakeCalendar 를 사용하세요 (--fake)."
        ) from exc

    p = Path(token_path)
    creds = None
    if p.exists():
        creds = Credentials.from_authorized_user_file(str(p), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise CalendarUnavailable(
                    f"{token_path} 갱신 실패: {exc}\n"
                    f"  테스트 모드 앱의 refresh token 은 7일 후 만료됩니다.\n"
                    f"  해결: python scripts/bootstrap_tokens.py"
                ) from exc
        else:
            if not Path(CREDENTIALS_FILE).exists():
                raise CalendarUnavailable(
                    f"{CREDENTIALS_FILE} 이 없습니다. "
                    f"Google Cloud Console 에서 OAuth 클라이언트(데스크톱 앱)를 "
                    f"만들어 프로젝트 루트에 저장하세요.")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(creds.to_json(), encoding="utf-8")

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


class GoogleCalendar:
    """실제 Google Calendar. FakeCalendar 와 동일한 인터페이스."""

    def __init__(self, token_path: str, tz: str = "Asia/Seoul"):
        self.token_path = token_path
        self.tz = tz
        self._service = None

    @property
    def service(self):
        if self._service is None:
            self._service = get_google_service(self.token_path)
        return self._service

    def fetch_busy(self, calendar_ids, time_min, time_max):
        """FreeBusy 만 사용한다. 일정 제목은 애초에 반환되지 않는다."""
        from googleapiclient.errors import HttpError

        body = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "timeZone": self.tz,
            "items": [{"id": cid} for cid in calendar_ids],
        }
        try:
            # num_retries: 오래 유휴였던 연결이 끊겨 있거나 일시적 5xx 가 나면 라이브러리가
            # 지수 백오프로 재시도한다. 조회 전용이라 재시도해도 부작용이 없다.
            resp = (self.service.freebusy().query(body=body)
                    .execute(num_retries=FREEBUSY_RETRIES))
        except CalendarUnavailable:
            raise
        except HttpError as exc:
            raise CalendarUnavailable(f"FreeBusy 조회 실패: {exc}") from exc
        except Exception as exc:                        # noqa: BLE001
            # 연결 오류·인증 갱신 오류·손상된 토큰 파일 등. HttpError 가 아니라고 해서
            # 그대로 올리면 에이전트가 원인 불명의 500 을 돌려준다. 어떤 실패든
            # '조회 실패'(fail-closed, 503)로 명확히 알리고, 다음 요청이 새 연결·새 인증
            # 상태로 시작하도록 캐시된 서비스를 버린다.
            self._service = None
            raise CalendarUnavailable(
                f"FreeBusy 조회 실패: {type(exc).__name__}: {str(exc)[:200]}") from exc

        out: dict[str, list[tuple]] = {}
        for cid, data in resp.get("calendars", {}).items():
            if data.get("errors"):
                raise CalendarUnavailable(f"캘린더 {cid} 오류: {data['errors']}")
            out[cid] = [
                (dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                 dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
                for b in data.get("busy", [])
            ]
        for cid in calendar_ids:
            out.setdefault(cid, [])
        return out

    def fetch_events(self, time_min, time_max):
        """일정 종류 분류에 필요한 최소 항목만 읽는다 (primary 캘린더).

        `fields` 로 start/end/colorId/transparency/eventType/status 만 요청한다. 제목·설명·장소·참석자·
        화상회의·첨부·이벤트 ID 는 요청하지 않으므로 응답에 없다. 반복 일정은 singleEvents=True 로 각 회차가
        따로 오므로 반복 규칙을 읽을 필요가 없다. 실패하면 CalendarUnavailable (빈 시간으로 처리하지 않는다).
        """
        from googleapiclient.errors import HttpError

        events: list[CalEvent] = []
        page_token = None
        try:
            for _ in range(EVENTS_MAX_PAGES):
                kwargs = dict(
                    calendarId="primary", timeMin=time_min.isoformat(), timeMax=time_max.isoformat(),
                    singleEvents=True, showDeleted=False, maxResults=EVENTS_PAGE_SIZE,
                    timeZone=self.tz, fields=EVENT_FIELDS)
                if page_token:
                    kwargs["pageToken"] = page_token
                resp = self.service.events().list(**kwargs).execute(num_retries=FREEBUSY_RETRIES)
                events += [self._parse_event(item) for item in resp.get("items", [])]
                page_token = resp.get("nextPageToken")
                if not page_token:
                    return events
        except CalendarUnavailable:
            raise
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            hint = (" (권한 부족일 수 있습니다: python scripts/bootstrap_tokens.py 로 다시 인증하세요)"
                    if status in (401, 403) else "")
            raise CalendarUnavailable(f"일정 조회 실패 ({status}){hint}") from exc
        except Exception as exc:                        # noqa: BLE001
            self._service = None
            raise CalendarUnavailable(f"일정 조회 실패: {type(exc).__name__}") from exc
        # 페이지가 너무 많다: 일부만 보고 '한가함'으로 판단하지 않는다
        raise CalendarUnavailable("일정이 너무 많아 전부 조회하지 못했습니다")

    def _parse_event(self, item: dict) -> CalEvent:
        def when(node) -> dt.datetime:
            if not isinstance(node, dict):
                raise CalendarUnavailable("일정 시각을 읽지 못했습니다")
            if node.get("dateTime"):
                return dt.datetime.fromisoformat(node["dateTime"].replace("Z", "+00:00"))
            if node.get("date"):                        # 종일 일정: 그날 0시 (끝은 다음 날 0시, 포함하지 않음)
                from zoneinfo import ZoneInfo
                d = dt.date.fromisoformat(node["date"])
                return dt.datetime.combine(d, dt.time(0), tzinfo=ZoneInfo(self.tz))
            raise CalendarUnavailable("일정 시각을 읽지 못했습니다")

        try:
            return CalEvent(
                when(item.get("start")), when(item.get("end")),
                color_id=str(item["colorId"]) if item.get("colorId") else None,
                transparency=item.get("transparency") or "opaque",
                event_type=item.get("eventType") or "default",
                status=item.get("status") or "confirmed")
        except (ValueError, TypeError) as exc:
            raise CalendarUnavailable("일정 시각을 읽지 못했습니다") from exc

    def commit_event(self, *, event_id, summary, start, end,
                     attendee_emails, description=""):
        """organizer 캘린더에 단 한 번만 생성한다.

        나머지 참가자는 attendees 로 초대되므로 이벤트도 하나, Meet 링크도
        하나, 알림도 한 번만 간다. 사용자별로 insert 를 돌리면 3중으로 생긴다.
        """
        from googleapiclient.errors import HttpError

        body = {
            "id": event_id,
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": self.tz},
            "end": {"dateTime": end.isoformat(), "timeZone": self.tz},
            "attendees": [{"email": e} for e in attendee_emails],
            "conferenceData": {
                "createRequest": {
                    "requestId": event_id,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
            "reminders": {"useDefault": True},
        }
        try:
            created = self.service.events().insert(
                calendarId="primary", body=body,
                conferenceDataVersion=1, sendUpdates="all",
            ).execute()
            return created["id"], True
        except HttpError as exc:
            if getattr(exc, "resp", None) is not None and exc.resp.status == 409:
                # 같은 ID의 이벤트가 이미 있음 = 이전 시도가 성공했음
                return event_id, False
            raise

    def update_event(self, *, event_id, start=None, end=None, attendee_emails=None):
        """기존 이벤트를 patch 한다 (새 이벤트를 만들지 않는다). 이벤트를 읽지 않는다.

        patch 는 배열을 통째로 바꾸므로 attendees 는 최종 목록 전체를 보낸다.
        """
        from googleapiclient.errors import HttpError

        body: dict = {}
        if start is not None and end is not None:
            body["start"] = {"dateTime": start.isoformat(), "timeZone": self.tz}
            body["end"] = {"dateTime": end.isoformat(), "timeZone": self.tz}
        if attendee_emails is not None:
            body["attendees"] = [{"email": e} for e in attendee_emails]
        if not body:
            return True
        try:
            self.service.events().patch(
                calendarId="primary", eventId=event_id, body=body, sendUpdates="all",
            ).execute()
            return True
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (404, 410):
                raise EventNotFound(f"이벤트 {event_id} 를 찾을 수 없습니다") from exc
            raise CalendarUnavailable(f"이벤트 수정 실패: {exc}") from exc
        except CalendarUnavailable:
            raise
        except Exception as exc:                                  # noqa: BLE001
            self._service = None
            raise CalendarUnavailable(
                f"이벤트 수정 실패: {type(exc).__name__}: {str(exc)[:200]}") from exc

    def cancel_event(self, event_id):
        """이벤트를 삭제(취소)한다. 이미 없으면 False — 다시 호출해도 안전하다."""
        from googleapiclient.errors import HttpError

        try:
            self.service.events().delete(
                calendarId="primary", eventId=event_id, sendUpdates="all").execute()
            return True
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status in (404, 410):
                return False
            raise CalendarUnavailable(f"이벤트 취소 실패: {exc}") from exc
        except CalendarUnavailable:
            raise
        except Exception as exc:                                  # noqa: BLE001
            self._service = None
            raise CalendarUnavailable(
                f"이벤트 취소 실패: {type(exc).__name__}: {str(exc)[:200]}") from exc


def make_backend(profile, fake: bool = False,
                 fake_spec: dict | None = None) -> CalendarBackend:
    """프로필과 모드에 맞는 백엔드를 만든다."""
    if fake:
        return FakeCalendar(profile.display_name, fake_spec or {})
    return GoogleCalendar(profile.token_path)
