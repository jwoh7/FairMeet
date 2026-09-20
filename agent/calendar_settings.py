"""일정 종류 설정: 사용자가 Google Calendar 색상을 '고정 / 유동 / 무시'로 나눈다.

Google Calendar 의 색상 자체에는 FairMeet 의 의미가 없다. 어떤 색상을 고정으로 쓸지는 사용자가 고른다.

분류 규칙 (classify_event)
  - 취소된 일정(cancelled)                → 무시
  - '한가함(free)' 으로 표시(transparent) → 무시 (충돌로 세지 않는다)
  - 부재 일정(outOfOffice)                → 고정
  - 색상이 있고 사용자가 정한 설정이 있으면 그 설정
  - 색상 미지정 · 알 수 없는 색상 · 설정 없음  → 고정 (안전한 쪽)
  - 조회 실패는 여기까지 오지 않는다 (CalendarUnavailable 로 협상이 중단된다: fail-closed)

고정: 절대 옮길 수 없다. 겹치면 거부권.  유동: 옮길 수 있다. 겹쳐도 후보에 남고 불편 비용만 더한다.

설정은 참가자의 개인 에이전트 쪽에만 저장된다 (중재자에게는 색상 번호와 종류만 오가고 일정 내용은 오가지 않는다).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("fixed", "flex", "ignore")
KIND_LABEL = {"fixed": "고정", "flex": "유동", "ignore": "무시"}

# Google Calendar 의 일정 색상 번호 (colorId 1~11). 표시 색은 근사값이다.
PALETTE = {
    "1": ("라벤더", "#a4bdfc"), "2": ("세이지", "#7ae7bf"), "3": ("포도", "#dbadff"),
    "4": ("플라밍고", "#ff887c"), "5": ("바나나", "#fbd75b"), "6": ("귤", "#ffb878"),
    "7": ("공작", "#46d6db"), "8": ("흑연", "#e1e1e1"), "9": ("블루베리", "#5484ed"),
    "10": ("바질", "#51b749"), "11": ("토마토", "#dc2127"),
}
DEFAULT_COLOR = "default"          # 색상을 지정하지 않은 일정 (캘린더 기본색)


class SettingsError(ValueError):
    """설정 저장 요청이 올바르지 않다. 메시지는 사용자에게 그대로 보인다."""


@dataclass
class CalendarSettings:
    colors: dict = field(default_factory=dict)       # {"5": "flex", "8": "ignore"}
    version: int = 0
    updated_at: str | None = None

    @property
    def configured(self) -> bool:
        """사용자가 색상 분류를 하나라도 설정했는가."""
        return bool(self.colors)

    def to_dict(self) -> dict:
        return {"colors": dict(self.colors), "version": self.version, "updated_at": self.updated_at}


def validate_mapping(pairs) -> dict:
    """[{"colorId": "5", "kind": "flex"}, ...] 를 {"5": "flex"} 로 검증·변환한다.

    같은 색상이 두 번 나오면(중복 매핑) 거절한다. '고정'으로 두는 색상도 기록해 둘 수 있다.
    """
    if isinstance(pairs, dict):                       # 이미 사전이면 목록으로 바꿔 같은 검증을 거친다
        pairs = [{"colorId": k, "kind": v} for k, v in pairs.items()]
    if not isinstance(pairs, list):
        raise SettingsError("색상 설정 형식이 올바르지 않습니다")
    if len(pairs) > len(PALETTE):
        raise SettingsError("색상 설정이 너무 많습니다")
    out: dict = {}
    for item in pairs:
        if not isinstance(item, dict):
            raise SettingsError("색상 설정 형식이 올바르지 않습니다")
        cid, kind = str(item.get("colorId", "")), item.get("kind")
        if cid not in PALETTE:
            raise SettingsError("알 수 없는 색상입니다")
        if kind not in KINDS:
            raise SettingsError("고정, 유동, 무시 중에서 골라 주세요")
        if cid in out:
            raise SettingsError(f"'{PALETTE[cid][0]}' 색상이 두 번 지정되었습니다. 한 색상은 한 가지로만 정할 수 있어요")
        out[cid] = kind
    return out


# ---- 저장소 ------------------------------------------------------------------------------------

class MemorySettings:
    """테스트·가짜 캘린더 모드용. 프로세스가 끝나면 사라진다."""

    def __init__(self, colors: dict | None = None):
        self._s = CalendarSettings(colors=dict(colors or {}), version=1 if colors else 0)
        self._mu = threading.Lock()

    def load(self) -> CalendarSettings:
        with self._mu:
            return CalendarSettings(dict(self._s.colors), self._s.version, self._s.updated_at)

    def save(self, colors: dict, expected_version: int | None = None) -> CalendarSettings:
        with self._mu:
            if expected_version is not None and expected_version != self._s.version:
                raise SettingsError("다른 곳에서 설정이 바뀌었어요. 화면을 새로고침한 뒤 다시 저장해 주세요")
            self._s = CalendarSettings(dict(colors), self._s.version + 1,
                                       dt.datetime.now(dt.timezone.utc).isoformat())
            return CalendarSettings(dict(self._s.colors), self._s.version, self._s.updated_at)


class FileSettings:
    """참가자별 JSON 파일. 파일 하나가 한 사람의 설정이라 사람 사이에 섞이지 않는다."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._mu = threading.Lock()

    def load(self) -> CalendarSettings:
        with self._mu:
            return self._read()

    def _read(self) -> CalendarSettings:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            colors = {str(k): v for k, v in (data.get("colors") or {}).items()
                      if str(k) in PALETTE and v in KINDS}
            return CalendarSettings(colors, int(data.get("version", 0)), data.get("updated_at"))
        except (OSError, ValueError, AttributeError):
            return CalendarSettings()                 # 없거나 깨진 파일 = 설정 없음 = 전부 고정 (안전한 쪽)

    def save(self, colors: dict, expected_version: int | None = None) -> CalendarSettings:
        with self._mu:
            cur = self._read()
            if expected_version is not None and expected_version != cur.version:
                raise SettingsError("다른 곳에서 설정이 바뀌었어요. 화면을 새로고침한 뒤 다시 저장해 주세요")
            new = CalendarSettings(dict(colors), cur.version + 1,
                                   dt.datetime.now(dt.timezone.utc).isoformat())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".settings.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(new.to_dict(), f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            return new


# ---- 분류 --------------------------------------------------------------------------------------

def classify_event(ev, settings: CalendarSettings) -> str | None:
    """이 일정이 어떤 종류인가. 'fixed' | 'flex' | None(충돌로 세지 않음)."""
    if ev.status == "cancelled":
        return None
    if ev.transparency == "transparent":
        return None
    if ev.event_type == "outOfOffice":
        return "fixed"
    cid = ev.color_id
    if cid is None or cid not in PALETTE:
        return "fixed"
    kind = settings.colors.get(cid, "fixed")
    if kind == "ignore":
        return None
    return "flex" if kind == "flex" else "fixed"


def classify_events(events, settings: CalendarSettings) -> list[tuple]:
    """[(start, end, kind)] — 무시할 일정은 뺀다."""
    out = []
    for ev in events:
        kind = classify_event(ev, settings)
        if kind is not None:
            out.append((ev.start, ev.end, kind))
    return out


def color_usage(events) -> dict:
    """일정 색상별 개수 (색상 선택 화면용). 제목·시간 같은 내용은 담지 않는다."""
    counts: dict = {}
    for ev in events:
        if ev.status == "cancelled":
            continue
        key = ev.color_id if ev.color_id in PALETTE else DEFAULT_COLOR
        counts[key] = counts.get(key, 0) + 1
    return counts
