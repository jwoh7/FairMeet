"""슬롯 그리드 생성. 인덱스가 곧 비용 벡터의 인덱스가 되므로,
모든 에이전트가 동일한 그리드를 써야 한다.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

# 모드별 탐색 윈도우. (요일범위, [(시작시, 종료시), ...])
MODE_WINDOWS = {
    # 회사: 평일 업무시간
    "work": {
        "weekday": [(9, 19)],
        "weekend": [],
    },
    # 친구: 평일 저녁 + 주말 낮~저녁
    "social": {
        "weekday": [(19, 23)],
        "weekend": [(11, 22)],
    },
}


def next_monday(today: dt.date | None = None) -> dt.date:
    """다음 주 월요일. 데모에서 탐색 시작일로 쓴다."""
    today = today or dt.datetime.now(KST).date()
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)


def build_slots(
    base: dt.date,
    mode: str = "work",
    duration_min: int = 60,
    grid_min: int = 30,
    days: int = 7,
    tz: ZoneInfo = KST,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """[(start, end), ...] 를 시간순으로 반환한다.

    리스트의 인덱스가 프로토콜 전체에서 슬롯 번호로 쓰이므로,
    같은 세션에 참여하는 모든 에이전트는 반드시 동일한 인자로 호출해야 한다.
    """
    if mode not in MODE_WINDOWS:
        raise ValueError(f"알 수 없는 모드: {mode} (work 또는 social)")

    win = MODE_WINDOWS[mode]
    slots: list[tuple[dt.datetime, dt.datetime]] = []

    for d in range(days):
        day = base + dt.timedelta(days=d)
        ranges = win["weekend"] if day.weekday() >= 5 else win["weekday"]
        for h0, h1 in ranges:
            cur = dt.datetime.combine(day, dt.time(h0), tzinfo=tz)
            limit = dt.datetime.combine(day, dt.time(h1), tzinfo=tz)
            step = dt.timedelta(minutes=grid_min)
            length = dt.timedelta(minutes=duration_min)
            while cur + length <= limit:
                slots.append((cur, cur + length))
                cur += step

    return slots


def format_slot(start: dt.datetime, end: dt.datetime) -> str:
    """사람이 읽는 형식. 승인 화면과 로그에 쓴다."""
    wd = "월화수목금토일"[start.weekday()]
    return f"{start:%m월 %d일}({wd}) {start:%H:%M}–{end:%H:%M}"
