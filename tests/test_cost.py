"""비용 함수 테스트. 고정 일정은 거부권, 유동 일정은 비용, 취향 점수(아침형/저녁형)와 JSON 보호시간은 없다."""
import datetime as dt

from agent.cost import FLEX_COST, MAX_COST, cost_vector
from agent.profile import Profile, validate_profile
from common.slots import KST, build_slots

MON = dt.date(2026, 9, 21)


def _profile(**kw) -> Profile:
    base = dict(user_key="t@x.com", display_name="테스트", email="t@x.com")
    base.update(kw)
    return Profile(**base)


def _slot(day_offset, hour):
    d = MON + dt.timedelta(days=day_offset)
    s = dt.datetime.combine(d, dt.time(hour), tzinfo=KST)
    return (s, s + dt.timedelta(hours=1))


def _fixed(slot):
    return (slot[0], slot[1], "fixed")


def _flex(slot):
    return (slot[0], slot[1], "flex")


def test_fixed_conflict_is_veto_not_cost():
    """기존 고정 일정 위에 회의를 얹을 수 있으면 안 된다."""
    slots = [_slot(0, 10)]
    costs, veto = cost_vector(slots, [_fixed(slots[0])])
    assert veto[0] is True and costs[0] is None


def test_flex_overlap_keeps_the_slot_and_only_costs():
    """유동으로 표시한 일정과 겹치면 후보에 남고 불편 비용만 든다."""
    s = _slot(0, 10)
    costs, veto = cost_vector([s], [_flex(s)])
    assert veto[0] is False and costs[0] is not None and costs[0] >= FLEX_COST


def test_flex_costs_more_than_free_but_less_than_the_veto():
    s_free, s_flex = _slot(1, 10), _slot(0, 10)
    costs, veto = cost_vector([s_free, s_flex], [_flex(s_flex)])
    assert costs[1] > costs[0] and veto == [False, False]


def test_a_fixed_and_a_flex_event_on_the_same_slot_is_still_a_veto():
    s = _slot(0, 14)
    costs, veto = cost_vector([s], [_flex(s), _fixed(s)])
    assert veto[0] is True


def test_sleep_protection_regardless_of_anything_else():
    d = MON
    s = dt.datetime.combine(d, dt.time(4), tzinfo=KST)
    costs, veto = cost_vector([(s, s + dt.timedelta(hours=1))], [])
    assert veto[0] is True
    late = dt.datetime.combine(d, dt.time(23), tzinfo=KST)
    assert cost_vector([(late, late + dt.timedelta(hours=1))], [])[1][0] is True


def test_costs_within_range():
    slots = build_slots(MON, "work", 60)
    costs, veto = cost_vector(slots, [])
    for c in costs:
        assert c is None or 0.0 <= c <= MAX_COST


def test_veto_mask_matches_none_costs():
    """프로토콜 불변식: veto 면 cost 는 반드시 None."""
    slots = build_slots(MON, "work", 60)
    s, e = slots[3]
    costs, veto = cost_vector(slots, [(s, e, "fixed")])
    assert veto[3] is True
    for c, v in zip(costs, veto):
        assert (c is None) == v


def test_no_chronotype_or_time_of_day_taste_exists():
    """아침형/저녁형 점수는 사라졌다: 같은 조건이면 오전 9시와 오후 4시의 비용이 같다."""
    nine, four = _slot(0, 9), _slot(0, 16)
    costs, _ = cost_vector([nine, four], [])
    assert costs[0] == costs[1]
    assert len({c for c in cost_vector(build_slots(MON, "work", 60), [])[0]}) == 1


def test_legacy_profile_keys_are_ignored_and_change_nothing(tmp_path=None):
    import json
    import tempfile
    from pathlib import Path
    base = {"user_key": "t@x.com", "display_name": "테스트", "email": "t@x.com"}
    variants = [dict(base), dict(base, chronotype="morning", mode="social", soft_busy_minutes_budget=10,
                                 hard_calendars=["care"], flexible_calendars=["x"],
                                 protected_windows=[[0, "00:00", "23:00", "보호"]]),
                dict(base, chronotype="evening", primary_calendars=["z"])]
    results = []
    for data in variants:
        p = Path(tempfile.mkdtemp()) / "p.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        prof = Profile.load(p)
        assert set(vars(prof)) == {"user_key", "display_name", "email", "token_path", "is_organizer"}
        results.append(cost_vector(build_slots(MON, "work", 60), [_fixed(_slot(0, 10))]))
    assert results[0] == results[1] == results[2]


def test_busy_minutes_not_block_count():
    """구간 개수가 아니라 '분'으로 측정한다: 짧은 구간 5개보다 긴 구간 1개가 더 큰 부담."""
    s, e = _slot(0, 16)
    d = MON
    many_short = [(dt.datetime.combine(d, dt.time(9 + i), tzinfo=KST),
                   dt.datetime.combine(d, dt.time(9 + i), tzinfo=KST) + dt.timedelta(minutes=5), "fixed")
                  for i in range(5)]
    one_long = [(dt.datetime.combine(d, dt.time(9), tzinfo=KST),
                 dt.datetime.combine(d, dt.time(15), tzinfo=KST), "fixed")]
    c_short, _ = cost_vector([(s, e)], many_short)
    c_long, _ = cost_vector([(s, e)], one_long)
    assert c_long[0] > c_short[0]


def test_a_busier_day_costs_more_without_any_personal_threshold():
    s = _slot(0, 16)
    light = [(_slot(0, 9)[0], _slot(0, 9)[1], "fixed")]
    heavy = light + [(_slot(0, h)[0], _slot(0, h)[1], "fixed") for h in (10, 11, 12, 13)]
    assert cost_vector([s], heavy)[0][0] > cost_vector([s], light)[0][0]


def test_an_isolated_meeting_costs_a_little_more_than_one_next_to_other_events():
    near = _slot(0, 11)
    far = _slot(1, 11)
    events = [(_slot(0, 9)[0], _slot(0, 10)[1], "fixed")]
    c_near, _ = cost_vector([near], events)
    c_far, _ = cost_vector([far], events)
    assert c_far[0] > 0 and c_near[0] != c_far[0]


def test_buffer_looks_only_at_fixed_neighbours_because_flex_events_can_move():
    from agent.cost import parse_buffer_options
    s = _slot(0, 11)
    neighbour_fixed = [(_slot(0, 10)[0], _slot(0, 10)[1], "fixed")]
    neighbour_flex = [(_slot(0, 10)[0], _slot(0, 10)[1], "flex")]
    hard = parse_buffer_options({"before_min": 15, "after_min": 0, "strength": "hard"})
    soft = parse_buffer_options({"before_min": 15, "after_min": 0, "strength": "soft"})
    assert cost_vector([s], neighbour_fixed, hard)[1][0] is True
    assert cost_vector([s], neighbour_fixed, soft)[0][0] > cost_vector([s], neighbour_fixed)[0][0]
    assert cost_vector([s], neighbour_flex, hard)[1][0] is False
    assert cost_vector([s], neighbour_flex, soft)[0][0] == cost_vector([s], neighbour_flex)[0][0]

# ---- 프로필 검증 (연결 정보만 검증한다) ---------------------------------------------------------------

def test_a_valid_minimal_profile_passes():
    assert validate_profile(_profile()) == []


def test_bad_email_rejected():
    p = _profile(email="not-an-email")
    assert any("이메일" in e for e in validate_profile(p))


def test_the_profile_no_longer_has_personal_preference_fields():
    fields = set(vars(_profile()))
    for gone in ("chronotype", "protected_windows", "soft_busy_minutes_budget", "hard_calendars",
                 "flexible_calendars", "primary_calendars", "mode"):
        assert gone not in fields, gone
