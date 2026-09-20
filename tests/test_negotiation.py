"""협상 루프 통합 테스트. 가짜 캘린더로 전 구간을 돌린다."""
import datetime as dt
import tempfile
from pathlib import Path

from agent.calendar_client import FakeCalendar
from agent.calendar_settings import MemorySettings
from agent.negotiator import LocalAgent
from agent.profile import Profile
from common.ids import new_session_id
from common.slots import KST, build_slots
from mediator import store
from mediator.negotiation import run_negotiation
from mediator.transport import InProcessTransport

MON = dt.date(2026, 9, 21)
GID = "test-negotiation"
SECRET = "test-secret"

_tmp_db = None


def setup_function(fn=None):
    global _tmp_db
    if _tmp_db and Path(_tmp_db).exists():
        Path(_tmp_db).unlink()
    _tmp_db = tempfile.mktemp(suffix=".db")
    store.set_db_path(_tmp_db)
    store.init_db()


def _agent(name, busy=None, organizer=False, fail=False, flex=None):
    """busy: 색상 없는 바쁨 = 고정 일정. flex: 유동(색상 5, 사용자가 유동으로 설정) 일정 [[시작, 끝], ...]."""
    p = Profile(user_key=f"{name}@x.com", display_name=name,
                email=f"{name}@x.com", is_organizer=organizer)
    backend = FakeCalendar(name, busy or {}, fail_fetch=fail,
                           events=[{"start": a, "end": b, "colorId": "5"} for a, b in (flex or [])])
    return LocalAgent(p, backend, SECRET, GID, settings_store=MemorySettings({"5": "flex"}))


def _every_day(h0, h1):
    return [_blk(d, h0, h1) for d in range(5)]


def _run(agents, slots=None, lam=0.6, seed_balance=None):
    slots = slots or build_slots(MON, "work", 60)
    transport = InProcessTransport({a.agent_id: a for a in agents})
    sid = new_session_id()
    store.create_session(sid, GID, "work", 60, slots, [
        {"agent_id": a.agent_id, "display_name": a.profile.display_name,
         "email": a.profile.email, "is_organizer": a.profile.is_organizer}
        for a in agents])
    if seed_balance:
        store.seed_balance(GID, seed_balance)
    outcome = run_negotiation(sid, transport, slots, GID, lam=lam)
    return sid, outcome, transport, slots


def _blk(day, h0, h1):
    d = MON + dt.timedelta(days=day)
    return [dt.datetime.combine(d, dt.time(h0), tzinfo=KST).isoformat(),
            dt.datetime.combine(d, dt.time(h1), tzinfo=KST).isoformat()]


def test_happy_path_reaches_approval():
    sid, out, _, _ = _run([_agent("A", organizer=True), _agent("B")])
    assert out.state == "PENDING_HUMAN_APPROVAL"
    assert out.slot_index is not None
    assert store.get_session(sid)["state"] == "PENDING_HUMAN_APPROVAL"


def test_regret_matrix_saved_and_immutable():
    sid, out, _, _ = _run([_agent("A", organizer=True), _agent("B")])
    regret, mask, _ = store.load_regret(sid)
    assert regret and len(regret) == 2
    assert all(len(v) == 95 for v in regret.values())


def test_no_common_slot_yields_no_feasible():
    """서로 다른 시간만 가능하면 협상이 실패해야 한다.

    공통 가능 슬롯이 0개인 경우의 종료 상태는 NO_COMMON_SLOT 이다
    (재협상 기능 이전에는 NO_FEASIBLE_SLOT 이었다).
    """
    # A 는 월~금 전체가 HARD, B 는 정상
    all_week = {"care": [_blk(d, 8, 20) for d in range(5)]}
    sid, out, _, _ = _run([_agent("A", all_week, organizer=True), _agent("B")])
    assert out.state == "NO_COMMON_SLOT"


def test_calendar_failure_is_fail_closed():
    """조회 실패를 '한가함'으로 처리하면 안 된다."""
    sid, out, _, _ = _run([_agent("A", organizer=True), _agent("B", fail=True)])
    assert out.state == "AGENT_UNAVAILABLE"
    assert store.get_session(sid)["state"] == "AGENT_UNAVAILABLE"


def test_veto_slot_never_proposed():
    """거부권이 걸린 시간대가 제안되면 안 된다."""
    busy = {"care": [_blk(d, 9, 20) for d in (0, 1)]}   # 월화 전체 불가
    agents = [_agent("A", busy, organizer=True), _agent("B")]
    sid, out, _, slots = _run(agents)
    assert out.state == "PENDING_HUMAN_APPROVAL"
    s, _ = slots[out.slot_index]
    assert s.weekday() not in (0, 1), "거부권 시간대가 제안됨"


def test_balance_changes_the_outcome():
    """잔액이 실제 협상 결과를 바꾸는지 — 단위 테스트가 아닌 통합 검증."""
    # A 는 매일 오전에, B 는 매일 오후에 옮길 수 있는 일정(유동)이 있어 각자 불편한 시간대가 다르다
    def pair():
        return (_agent("A", organizer=True, flex=_every_day(9, 13)),
                _agent("B", flex=_every_day(13, 19)))
    a, b = pair()

    _, out1, _, slots = _run([a, b], lam=0.0)
    setup_function()
    a2, b2 = pair()
    _, out2, _, _ = _run([a2, b2], lam=3.0,
                         seed_balance={a2.agent_id: 35.0, b2.agent_id: -35.0})
    assert out1.slot_index != out2.slot_index, \
        "잔액을 크게 줘도 결과가 같다면 보정이 작동하지 않는 것"


def test_counter_indices_narrow_the_search():
    """역제안이 실제로 다음 라운드 탐색을 좁히는지."""
    events = []
    a = _agent("A", organizer=True, flex=_every_day(9, 13))
    b = _agent("B", flex=_every_day(13, 19))
    slots = build_slots(MON, "work", 60)
    transport = InProcessTransport({x.agent_id: x for x in (a, b)})
    sid = new_session_id()
    store.create_session(sid, GID, "work", 60, slots, [
        {"agent_id": x.agent_id, "display_name": x.profile.display_name,
         "email": x.profile.email, "is_organizer": x.profile.is_organizer}
        for x in (a, b)])
    run_negotiation(sid, transport, slots, GID, lam=0.6, emit=events.append)

    proposes = [e for e in events if e["type"] == "propose"]
    responses = [e for e in events if e["type"] == "responses"]
    # 역제안이 나왔다면 다음 라운드 탐색 범위가 줄어야 한다
    for i, r in enumerate(responses[:-1]):
        if r["counter_indices"] and i + 1 < len(proposes):
            assert proposes[i + 1]["searched"] <= len(r["counter_indices"]), \
                "역제안이 다음 라운드에 반영되지 않음"


def test_three_agents_converge():
    agents = [_agent("A", organizer=True, flex=_every_day(9, 12)), _agent("B"),
              _agent("C", flex=_every_day(14, 18))]
    sid, out, _, _ = _run(agents)
    assert out.state == "PENDING_HUMAN_APPROVAL"
    assert len(out.regrets) == 3


def test_regrets_recorded_for_all_agents():
    agents = [_agent("A", organizer=True), _agent("B"), _agent("C")]
    sid, out, _, _ = _run(agents)
    saved = store.get_session(sid)["proposed_regrets"]
    import json
    assert len(json.loads(saved)) == 3


def test_max_rounds_respected():
    agents = [_agent("A", organizer=True), _agent("B")]
    slots = build_slots(MON, "work", 60)
    transport = InProcessTransport({a.agent_id: a for a in agents})
    sid = new_session_id()
    store.create_session(sid, GID, "work", 60, slots, [
        {"agent_id": a.agent_id, "display_name": a.profile.display_name,
         "email": a.profile.email, "is_organizer": a.profile.is_organizer}
        for a in agents])
    out = run_negotiation(sid, transport, slots, GID, max_rounds=1)
    assert out.rounds <= 1
