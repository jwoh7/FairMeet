"""FairMeet 발표용 Google Calendar 3계정 초기 상태를 멱등적으로 복원한다.

FairMeet 서버나 협상 모듈을 import하지 않는 독립 관리 도구다. 프로젝트에서는
Google 라이브러리가 설치된 .venv와 이미 발급된 토큰·프로필·색상 설정 경로만 쓴다.
매 실행 시 세 계정의 기본 캘린더에서 '다음 주 월요일 00:00 ~ 그다음 월요일 00:00'에
걸친 기존 일정을 먼저 전부 삭제하고, 발표용 초기 상태만 새로 만든다. 다른 주와
보조 캘린더는 건드리지 않는다.

색상 규칙
  토마토(11)  -> 고정: 겹치면 후보에서 제외
  바나나(5)  -> 유동: 후보에는 남지만 불편 비용 반영
  바질(10)   -> 무시: Google의 '한가함' 표시도 함께 적용
  색상 없음   -> 안전을 위해 고정(fail-closed)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("FAIRMEET_ROOT") or Path(__file__).resolve().parent.parent).resolve()

KST = ZoneInfo("Asia/Seoul")
PROFILE_FILES = {"a": "a.json", "b": "b.json", "c": "c.json"}
COLOR_SETTINGS = {"11": "fixed", "5": "flex", "10": "ignore"}
MANIFEST_PATH = ROOT / "settings" / "demo_calendar_manifest.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.events.owned",
]


def at(base: dt.date, day: int, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(base + dt.timedelta(days=day), dt.time(hour, minute), tzinfo=KST)


def event(event_id: str, day: int, start: tuple[int, int], end: tuple[int, int],
          summary: str, *, color: str | None = None,
          transparency: str = "opaque") -> dict:
    return {
        "id": event_id,
        "day": day,
        "start": start,
        "end": end,
        "summary": summary,
        "colorId": color,
        "transparency": transparency,
    }


# id는 시연 항목의 논리 ID다. 실제 Google ID는 manifest에 별도로 저장한다.
PLAN = {
    "a": [
        event("fmdemoa001", 0, (9, 0), (10, 30), "[FairMeet 시연] 전공 수업", color="11"),
        event("fmdemoa002", 0, (13, 0), (14, 0), "[FairMeet 시연] 자료 정리(이동 가능)", color="5"),
        event("fmdemoa003", 0, (16, 0), (17, 0), "[FairMeet 시연] 개인 메모(한가함)", color="10", transparency="transparent"),
        event("fmdemoa004", 1, (14, 0), (15, 0), "[FairMeet 시연] 팀 프로젝트", color="11"),
        event("fmdemoa005", 2, (10, 0), (12, 0), "[FairMeet 시연] 실험 수업", color="11"),
        event("fmdemoa006", 3, (15, 0), (18, 0), "[FairMeet 시연] 돌봄 시간", color="11"),
        event("fmdemoa007", 4, (11, 0), (12, 0), "[FairMeet 시연] 색상 미지정 일정"),
    ],
    "b": [
        event("fmdemob001", 0, (10, 0), (11, 0), "[FairMeet 시연] 전공 수업", color="11"),
        event("fmdemob002", 0, (14, 0), (15, 0), "[FairMeet 시연] 과제 시간(이동 가능)", color="5"),
        event("fmdemob003", 1, (9, 0), (11, 0), "[FairMeet 시연] 발표 준비", color="11"),
        event("fmdemob004", 1, (18, 0), (22, 0), "[FairMeet 시연] 아르바이트", color="11"),
        event("fmdemob005", 2, (13, 0), (14, 0), "[FairMeet 시연] 복습(이동 가능)", color="5"),
        event("fmdemob006", 3, (14, 0), (16, 0), "[FairMeet 시연] 세미나", color="11"),
        event("fmdemob007", 4, (13, 0), (14, 0), "[FairMeet 시연] 개인 할 일(한가함)", color="10", transparency="transparent"),
    ],
    "c": [
        event("fmdemoc001", 0, (11, 0), (12, 0), "[FairMeet 시연] 상담", color="11"),
        event("fmdemoc002", 0, (15, 0), (16, 0), "[FairMeet 시연] 독서(이동 가능)", color="5"),
        event("fmdemoc003", 1, (10, 0), (11, 0), "[FairMeet 시연] 전공 수업", color="11"),
        event("fmdemoc004", 2, (15, 0), (16, 0), "[FairMeet 시연] 병원 예약", color="11"),
        event("fmdemoc005", 3, (14, 0), (15, 0), "[FairMeet 시연] 운동(이동 가능)", color="5"),
        event("fmdemoc006", 4, (9, 0), (12, 0), "[FairMeet 시연] 현장 실습", color="11"),
        event("fmdemoc007", 4, (16, 0), (17, 0), "[FairMeet 시연] 개인 메모(한가함)", color="10", transparency="transparent"),
    ],
}

# 첫 버전 스크립트가 만든 ID. Google은 삭제한 커스텀 ID의 즉시 재사용을 막으므로
# 이 ID는 정리만 하고 새 일정에는 다시 쓰지 않는다.
LEGACY_EVENT_IDS = {
    label: [spec["id"] for spec in specs] for label, specs in PLAN.items()
}


def next_monday() -> dt.date:
    today = dt.datetime.now(KST).date()
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)


def profile_and_settings(label: str) -> tuple[dict, Path]:
    path = ROOT / "profiles" / PROFILE_FILES[label]
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"profiles/{PROFILE_FILES[label]} 파일을 읽을 수 없습니다") from exc
    for key in ("user_key", "display_name", "email", "token_path"):
        if not profile.get(key):
            raise RuntimeError(f"profiles/{PROFILE_FILES[label]}의 {key} 값이 비어 있습니다")
    if "@" not in str(profile["email"]):
        raise RuntimeError(f"profiles/{PROFILE_FILES[label]}의 이메일 형식이 올바르지 않습니다")
    token = ROOT / profile["token_path"]
    if not token.is_file():
        raise RuntimeError(f"{label} 계정 토큰 파일이 없습니다. scripts/bootstrap_tokens.py를 먼저 실행하세요")
    digest = hashlib.sha256(profile["user_key"].encode("utf-8")).hexdigest()[:16]
    return profile, ROOT / "settings" / f"{digest}.json"


def google_service(token_path: Path):
    """저장된 OAuth 토큰으로 Calendar API를 연다. 새 권한이나 브라우저 로그인을 요구하지 않는다."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        else:
            raise RuntimeError("Google 토큰이 유효하지 않습니다. scripts/bootstrap_tokens.py를 실행하세요")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def save_color_settings(path: Path) -> None:
    """FairMeet이 토마토/바나나/바질을 고정/유동/무시로 판단하도록 저장한다."""
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        version = int(old.get("version", 0)) + 1
    except (OSError, ValueError, AttributeError, TypeError):
        version = 1
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    atomic_json(path, {"colors": COLOR_SETTINGS, "version": version, "updated_at": now})


def delete_known(service, event_id: str) -> bool:
    from googleapiclient.errors import HttpError

    try:
        service.events().delete(calendarId="primary", eventId=event_id, sendUpdates="none").execute()
        return True
    except HttpError as exc:
        if getattr(getattr(exc, "resp", None), "status", None) in (404, 410):
            return False
        raise


def wipe_demo_week(service, base: dt.date) -> int:
    """기본 캘린더의 다음 주 일정을 전부 지운다. 제목·내용은 요청하지 않는다.

    sendUpdates=none 이므로 기존 FairMeet 초대 일정이 포함돼도 삭제 알림 메일을
    추가로 발송하지 않는다. 목록에는 ID만 요청한다.
    """
    response = service.events().list(
        calendarId="primary",
        timeMin=at(base, 0, 0).isoformat(),
        timeMax=at(base, 7, 0).isoformat(),
        singleEvents=True,
        showDeleted=False,
        fields="items(id)",
        maxResults=2500,
    ).execute()
    removed = 0
    for item in response.get("items", []):
        event_id = item.get("id")
        if event_id and delete_known(service, event_id):
            removed += 1
    return removed


def event_body(base: dt.date, spec: dict) -> dict:
    sh, sm = spec["start"]
    eh, em = spec["end"]
    body = {
        "summary": spec["summary"],
        "description": "FairMeet 해커톤 시연용 일정입니다. reset_demo_calendars.bat로 안전하게 복원할 수 있습니다.",
        "start": {"dateTime": at(base, spec["day"], sh, sm).isoformat(), "timeZone": "Asia/Seoul"},
        "end": {"dateTime": at(base, spec["day"], eh, em).isoformat(), "timeZone": "Asia/Seoul"},
        "transparency": spec["transparency"],
        "visibility": "private",
        "reminders": {"useDefault": False, "overrides": []},
    }
    if spec["colorId"]:
        body["colorId"] = spec["colorId"]
    return body


def new_google_id() -> str:
    # UUID hex는 Google custom ID가 허용하는 a-v/0-9의 부분집합이다.
    return "fmdemo" + uuid.uuid4().hex


def upsert_one(service, base: dt.date, spec: dict, current_id: str | None) -> str:
    from googleapiclient.errors import HttpError

    body = event_body(base, spec)
    if current_id:
        try:
            service.events().update(
                calendarId="primary", eventId=current_id, body=body, sendUpdates="none"
            ).execute()
            return current_id
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) not in (404, 410):
                raise

    # 사용자가 시연 일정을 직접 삭제했어도 새 ID로 다시 만들 수 있다.
    for _ in range(3):
        event_id = new_google_id()
        try:
            service.events().insert(
                calendarId="primary", body={"id": event_id, **body}, sendUpdates="none"
            ).execute()
            return event_id
        except HttpError as exc:
            if getattr(getattr(exc, "resp", None), "status", None) != 409:
                raise
    raise RuntimeError("새 시연 일정 ID를 발급하지 못했습니다")


def load_manifest() -> dict:
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        return {label: list(data.get(label) or []) for label in PROFILE_FILES}
    except (OSError, ValueError, AttributeError):
        return {label: [] for label in PROFILE_FILES}


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".demo-calendar.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_manifest(manifest: dict) -> None:
    atomic_json(MANIFEST_PATH, manifest)


def verify(service, base: dt.date, expected_ids: list[str]) -> dict:
    # 내용은 출력하지 않고, 우리가 만든 기간의 ID/색상/한가함 상태만 확인한다.
    response = service.events().list(
        calendarId="primary",
        timeMin=at(base, 0, 0).isoformat(),
        timeMax=at(base, 7, 0).isoformat(),
        singleEvents=True,
        showDeleted=False,
        fields="items(id,colorId,transparency)",
        maxResults=250,
    ).execute()
    ids = {item.get("id"): item for item in response.get("items", [])}
    found = {eid: ids[eid] for eid in expected_ids if eid in ids}
    return {
        "count": len(found),
        "fixed": sum(1 for eid, item in found.items() if item.get("colorId") == "11"),
        "flex": sum(1 for eid, item in found.items() if item.get("colorId") == "5"),
        "ignored": sum(1 for eid, item in found.items()
                       if item.get("colorId") == "10" and item.get("transparency") == "transparent"),
        "uncolored": sum(1 for eid, item in found.items() if not item.get("colorId")),
    }


def main() -> int:
    if sys.platform == "win32":
        # PowerShell의 기본 CP949 코드페이지와 무관하게 한글 진행 상황을 읽을 수 있게 한다.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="FairMeet 실제 Google Calendar 시연 일정 복원")
    parser.add_argument("--dry-run", action="store_true", help="Google Calendar를 변경하지 않고 계획만 검증")
    args = parser.parse_args()
    base = next_monday()

    print(f"FairMeet 시연 기준 주: {base.isoformat()} ~ {(base + dt.timedelta(days=6)).isoformat()}")
    print("대상: 재원(a), 도연(b), 승현(c) / 위 기간의 기본 캘린더 일정을 전부 초기화합니다.")
    if args.dry_run:
        total = sum(len(v) for v in PLAN.values())
        print(f"DRY RUN OK: {total}개 시연 일정, 색상 규칙 3개를 복원할 예정입니다.")
        return 0

    manifest = load_manifest()
    for label in ("a", "b", "c"):
        profile, settings_path = profile_and_settings(label)
        service = google_service(ROOT / profile["token_path"])
        removed = wipe_demo_week(service, base)
        # 위에서 그 주의 모든 일정을 지웠으므로, 삭제된 ID를 재사용하지 않고 새 ID로 만든다.
        current = []
        restored = []
        for i, spec in enumerate(PLAN[label]):
            restored.append(upsert_one(service, base, spec, current[i] if i < len(current) else None))
        manifest[label] = restored
        save_manifest(manifest)  # 계정마다 저장해 중간 장애 후에도 다음 실행으로 복구 가능
        save_color_settings(settings_path)
        result = verify(service, base, restored)
        if result["count"] != len(PLAN[label]):
            raise RuntimeError(f"{profile['display_name']}: 생성 검증 실패 ({result['count']}/{len(PLAN[label])})")
        print(
            f"OK {profile['display_name']}: 기존 일정 {removed}개 삭제, "
            f"{result['count']}개 복원 "
            f"(고정 {result['fixed']}, 유동 {result['flex']}, 무시 {result['ignored']}, "
            f"색상 없음 {result['uncolored']})"
        )

    print("\n복원 완료. FairMeet 서버를 재시작할 필요는 없습니다.")
    print("추천 시연 요청: 다음 주 평일 오후에 재원, 도연, 승현과 60분 프로젝트 회의를 잡아줘")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
