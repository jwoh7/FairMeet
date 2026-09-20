"""슬롯 그리드 테스트."""
import datetime as dt
from zoneinfo import ZoneInfo

from common.slots import KST, build_slots, format_slot, next_monday

MON = dt.date(2026, 9, 21)      # 월요일


def test_work_mode_excludes_weekend():
    slots = build_slots(MON, "work", 60)
    assert all(s.weekday() < 5 for s, _ in slots)


def test_work_mode_hours():
    slots = build_slots(MON, "work", 60)
    assert all(9 <= s.hour and e.hour <= 19 for s, e in slots)


def test_social_mode_includes_weekend():
    slots = build_slots(MON, "social", 120)
    assert any(s.weekday() >= 5 for s, _ in slots)


def test_slot_duration_is_exact():
    slots = build_slots(MON, "work", 90)
    assert all((e - s) == dt.timedelta(minutes=90) for s, e in slots)


def test_slots_are_ordered():
    slots = build_slots(MON, "work", 60)
    starts = [s for s, _ in slots]
    assert starts == sorted(starts)


def test_slots_are_timezone_aware():
    slots = build_slots(MON, "work", 60)
    assert all(s.tzinfo is not None for s, _ in slots)


def test_index_is_stable_across_calls():
    """모든 에이전트가 같은 인덱스를 봐야 프로토콜이 성립한다."""
    a = build_slots(MON, "work", 60)
    b = build_slots(MON, "work", 60)
    assert a == b


def test_unknown_mode_raises():
    try:
        build_slots(MON, "party", 60)
    except ValueError:
        return
    raise AssertionError("알 수 없는 모드가 통과됨")


def test_next_monday_is_future_monday():
    d = next_monday(dt.date(2026, 9, 19))       # 토요일
    assert d.weekday() == 0 and d > dt.date(2026, 9, 19)
    d2 = next_monday(dt.date(2026, 9, 21))      # 월요일 → 다음 주
    assert d2 == dt.date(2026, 9, 28)


def test_format_slot_is_korean():
    s = dt.datetime(2026, 9, 23, 14, 0, tzinfo=KST)
    e = dt.datetime(2026, 9, 23, 15, 0, tzinfo=KST)
    out = format_slot(s, e)
    assert "09월 23일" in out and "수" in out and "14:00" in out
