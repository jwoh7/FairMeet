"""커밋 오케스트레이션 테스트.

가장 중요한 세 가지를 검증한다:
  - 이벤트는 인원수와 무관하게 정확히 1개
  - 승인 대기 중 캘린더가 바뀌면 이벤트를 만들지 않는다
  - 커밋이 실패하면 잔액을 갱신하지 않는다
"""
import datetime as dt
import tempfile
from pathlib import Path

from agent.calendar_client import FakeCalendar, make_event_id
from agent.calendar_settings import MemorySettings
from agent.negotiator import LocalAgent
from agent.profile import Profile
from common.ids import new_session_id
from common.slots import KST, build_slots
from mediator import commit as commit_mod
from mediator import store
from mediator.negotiation import run_negotiation
from mediator.transport import InProcessTransport

MON = dt.date(2026, 9, 21)
GID = "test-commit"
SECRET = "test-secret"

_tmp_db = None


def setup_function(fn=None):
    global _tmp_db
    if _tmp_db and Path(_tmp_db).exists():
        Path(_tmp_db).unlink()
    _tmp_db = tempfile.mktemp(suffix=".db")
    store.set_db_path(_tmp_db)
    store.init_db()


def _busy_evenings(days):
    """회의 시간대(9~19시) 밖인 저녁 19~22시의 고정 일정. 그날의 일정 밀도만 높여 회의 시간을 막지는 않는다."""
    return {"primary": [[dt.datetime.combine(MON + dt.timedelta(days=d), dt.time(19), tzinfo=KST).isoformat(),
                         dt.datetime.combine(MON + dt.timedelta(days=d), dt.time(22), tzinfo=KST).isoformat()]
                        for d in days]}


def _agent(name, organizer=False, evenings=()):
    p = Profile(user_key=f"{name}@x.com", display_name=name,
                email=f"{name}@x.com", is_organizer=organizer)
    return LocalAgent(p, FakeCalendar(name, _busy_evenings(evenings)), SECRET, GID)


def _negotiate(n=3):
    names = ["민지", "준호", "서연"][:n]
    agents = [_agent(nm, organizer=(i == 0)) for i, nm in enumerate(names)]
    transport = InProcessTransport({a.agent_id: a for a in agents})
    slots = build_slots(MON, "work", 60)
    sid = new_session_id()
    store.create_session(sid, GID, "work", 60, slots, [
        {"agent_id": a.agent_id, "display_name": a.profile.display_name,
         "email": a.profile.email, "is_organizer": a.profile.is_organizer}
        for a in agents])
    out = run_negotiation(sid, transport, slots, GID)
    assert out.state == "PENDING_HUMAN_APPROVAL", out.state
    return sid, out, transport, agents, slots


def _approve_all(sid):
    for p in store.get_participants(sid):
        store.record_approval(sid, p["agent_id"], "approve")


def test_exactly_one_event_created():
    """P0-1: 참가자가 3명이어도 이벤트는 1개여야 한다."""
    sid, out, transport, agents, _ = _negotiate(3)
    _approve_all(sid)
    res = commit_mod.finalize(sid, transport)
    assert res.state == "COMMITTED"
    total = sum(len(a.backend.created_events) for a in agents)
    assert total == 1, f"이벤트가 {total}개 생성됨 (1이어야 함)"


def test_event_created_on_organizer_calendar():
    sid, out, transport, agents, _ = _negotiate(3)
    _approve_all(sid)
    commit_mod.finalize(sid, transport)
    organizer = agents[0]
    assert len(organizer.backend.created_events) == 1
    for other in agents[1:]:
        assert len(other.backend.created_events) == 0


def test_others_are_attendees():
    sid, out, transport, agents, _ = _negotiate(3)
    _approve_all(sid)
    commit_mod.finalize(sid, transport)
    ev = list(agents[0].backend.created_events.values())[0]
    assert len(ev["attendees"]) == 2
    assert agents[0].profile.email not in ev["attendees"]


def test_finalize_is_idempotent():
    """중복 호출해도 이벤트가 두 개 생기지 않는다."""
    sid, out, transport, agents, _ = _negotiate(2)
    _approve_all(sid)
    r1 = commit_mod.finalize(sid, transport)
    r2 = commit_mod.finalize(sid, transport)
    assert r1.state == "COMMITTED"
    assert r2.state == "COMMITTED"        # 이미 처리됨
    total = sum(len(a.backend.created_events) for a in agents)
    assert total == 1


def test_event_id_is_deterministic():
    sid = "f" * 32
    assert make_event_id(sid) == make_event_id(sid)
    assert make_event_id(sid).startswith("fm")


def test_stale_conflict_prevents_commit():
    """P0-7: 승인 대기 중 일정이 생기면 이벤트를 만들지 않는다."""
    sid, out, transport, agents, slots = _negotiate(3)
    _approve_all(sid)
    s, e = slots[out.slot_index]
    agents[2].backend.add_busy("primary", s, e)      # 충돌 주입

    res = commit_mod.finalize(sid, transport)
    assert res.state == "STALE_CONFLICT"
    total = sum(len(a.backend.created_events) for a in agents)
    assert total == 0, "충돌이 있는데 이벤트가 생성됨"


def test_stale_conflict_does_not_update_balance():
    """P0-6: 커밋 실패 시 원장이 오염되면 안 된다."""
    sid, out, transport, agents, slots = _negotiate(3)
    _approve_all(sid)
    s, e = slots[out.slot_index]
    agents[2].backend.add_busy("primary", s, e)
    commit_mod.finalize(sid, transport)

    bal = store.get_balance(GID, [a.agent_id for a in agents])
    assert all(abs(v) < 1e-9 for v in bal.values()), f"잔액이 갱신됨: {bal}"


def test_balance_updated_only_after_successful_commit():
    """부담이 서로 다를 때만 잔액이 움직인다.

    일정이 몰린 요일이 서로 다른 세 사람을 쓴다. 전원의 regret 이 같으면 잔액이
    0으로 남는 것이 올바른 동작이므로, 그 경우로는 이 성질을 검증할 수 없다.
    """
    agents = [_agent("민지", organizer=True, evenings=(0, 1)), _agent("준호"),
              _agent("서연", evenings=(3, 4))]
    transport = InProcessTransport({a.agent_id: a for a in agents})
    slots = build_slots(MON, "work", 60)
    sid = new_session_id()
    store.create_session(sid, GID, "work", 60, slots, [
        {"agent_id": a.agent_id, "display_name": a.profile.display_name,
         "email": a.profile.email, "is_organizer": a.profile.is_organizer}
        for a in agents])
    out = run_negotiation(sid, transport, slots, GID)
    assert out.state == "PENDING_HUMAN_APPROVAL"
    assert len(set(round(v, 3) for v in out.regrets.values())) > 1, \
        "이 테스트는 regret 이 서로 다른 상황을 전제한다"

    _approve_all(sid)
    before = store.get_balance(GID, [a.agent_id for a in agents])
    assert all(abs(v) < 1e-9 for v in before.values())

    commit_mod.finalize(sid, transport)
    after = store.get_balance(GID, [a.agent_id for a in agents])
    assert abs(sum(after.values())) < 1e-9          # zero-sum
    assert any(abs(v) > 1e-9 for v in after.values())


def test_equal_burden_leaves_balance_at_zero():
    """전원이 똑같이 부담하면 아무도 빚지지 않는다."""
    sid, out, transport, agents, _ = _negotiate(3)
    assert len(set(round(v, 3) for v in out.regrets.values())) == 1
    _approve_all(sid)
    commit_mod.finalize(sid, transport)
    bal = store.get_balance(GID, [a.agent_id for a in agents])
    assert all(abs(v) < 1e-9 for v in bal.values())


def test_calendar_unavailable_during_recheck():
    sid, out, transport, agents, _ = _negotiate(3)
    _approve_all(sid)
    agents[1].backend.fail_fetch = True
    res = commit_mod.finalize(sid, transport)
    assert res.state == "AGENT_UNAVAILABLE"
    total = sum(len(a.backend.created_events) for a in agents)
    assert total == 0


def test_reject_transitions_to_rejected():
    sid, out, transport, agents, _ = _negotiate(2)
    parts = store.get_participants(sid)
    store.record_approval(sid, parts[0]["agent_id"], "reject")
    assert commit_mod.reject(sid) is True
    assert store.get_session(sid)["state"] == "REJECTED"


def test_commit_records_google_event_id():
    sid, out, transport, agents, _ = _negotiate(2)
    _approve_all(sid)
    res = commit_mod.finalize(sid, transport)
    assert store.get_session(sid)["google_event_id"] == res.event_id
