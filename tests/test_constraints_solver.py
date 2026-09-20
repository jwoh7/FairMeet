"""회의 조건이 solver·에이전트·협상 루프에 실제로 반영되는지.

  - soft 페널티는 슬롯 선택에만 영향을 주고 개인 regret/잔액에는 섞이지 않는다.
  - hard 로 제외된 슬롯은 처음 협상에서도, 거절 뒤 재제안에서도 다시 나오지 않는다.
  - 회의 전후 여유는 에이전트가 자기 캘린더로 처리하고, 옵션이 없으면 예전과 똑같다.
"""
import datetime as dt
import json
import random

from agent.calendar_client import FakeCalendar
from agent.cost import cost_vector, parse_buffer_options
from agent.negotiator import LocalAgent
from agent.profile import Profile
from common.fairness import build_regret_matrix, solve
from common.ids import new_session_id
from common.slots import KST, build_slots
from mediator import approval_flow, store
from mediator import policy as policy_mod
from mediator.negotiation import run_negotiation
from mediator.transport import InProcessTransport
from tests.flow_helpers import GID, MON, SECRET, blk, fresh_db, make_agent


def setup_function(fn=None):
    fresh_db()


class _Raises:
    """pytest 없이도 돌아가는 pytest.raises 대체 (대체 러너 호환)."""

    def __init__(self, exc):
        self.exc, self.value = exc, None

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        if t is None:
            raise AssertionError(f"{self.exc.__name__} 예외가 나야 한다")
        if not issubclass(t, self.exc):
            return False
        self.value = v
        return True


# ---- solve() 의 slot_penalty ---------------------------------------------------------------

def _rand_regret(rng, agents=3, slots=12):
    r = {f"a{i}": [None if rng.random() < 0.2 else round(rng.uniform(0, 60), 1)
                   for _ in range(slots)] for i in range(agents)}
    for a in r:
        if all(v is None for v in r[a]):
            r[a][0] = 0.0
    return r


def test_default_behavior_is_unchanged_without_a_penalty():
    rng = random.Random(7)
    for _ in range(200):
        regret = _rand_regret(rng)
        balance = {a: rng.uniform(-20, 20) for a in regret}
        mask = set(range(12))
        base = solve(regret, balance, mask, lam=0.6)
        assert solve(regret, balance, mask, lam=0.6, slot_penalty=None) == base
        assert solve(regret, balance, mask, lam=0.6, slot_penalty={}) == base
        assert solve(regret, balance, mask, lam=0.6, slot_penalty={0: 0.0, 5: 0.0}) == base
        if base:
            assert base.penalty == 0.0


def test_penalty_steers_the_choice_but_never_the_reported_regrets():
    regret = {"a": [10.0, 10.0, 30.0], "b": [10.0, 10.0, 30.0]}
    plain = solve(regret, {}, {0, 1, 2})
    assert plain.slot_index == 0                              # 동점이면 앞의 슬롯
    steered = solve(regret, {}, {0, 1, 2}, slot_penalty={0: 12.0})
    assert steered.slot_index == 1 and steered.penalty == 0.0
    assert steered.regrets == {"a": 10.0, "b": 10.0}          # 페널티는 regret 에 섞이지 않는다
    assert steered.max_adjusted == 10.0 and steered.sum_regret == 20.0

    # 큰 페널티가 있는 슬롯이라도 다른 후보가 없으면 (hard 가 아니므로) 선택될 수 있다
    only = solve(regret, {}, {0}, slot_penalty={0: 50.0})
    assert only.slot_index == 0 and only.penalty == 50.0 and only.regrets == plain.regrets


def test_penalty_hits_everyone_equally_so_it_cannot_favor_one_participant():
    """같은 슬롯 집합에서 페널티가 참가자 간 순위를 바꾸지 않는다."""
    rng = random.Random(3)
    for _ in range(100):
        regret = _rand_regret(rng)
        mask = set(range(12))
        pen = {i: rng.uniform(0, 40) for i in range(12)}
        res = solve(regret, {}, mask, slot_penalty=pen)
        if res is None:
            continue
        s = res.slot_index
        assert res.regrets == {a: float(regret[a][s]) for a in regret}       # 순수 regret 그대로


# ---- 에이전트: 회의 전후 여유 ----------------------------------------------------------------------

def _prof(**kw):
    return Profile(user_key="u@x.com", display_name="테스터", email="u@x.com", **kw)


def _dtm(day, h, m=0):
    d = MON + dt.timedelta(days=day)
    return dt.datetime.combine(d, dt.time(h, m), tzinfo=KST)


def _slots(*starts, minutes=60):
    return [(s, s + dt.timedelta(minutes=minutes)) for s in starts]


BUSY = [(_dtm(0, 10), _dtm(0, 11), "fixed")]                     # 월 10:00~11:00 고정 일정


def test_parse_buffer_options_validation():
    assert parse_buffer_options(None) is None and parse_buffer_options({}) is None
    assert parse_buffer_options({"before_min": 0, "after_min": 0}) is None
    assert parse_buffer_options({"before_min": 15, "after_min": 30, "strength": "hard"}) == {
        "before_min": 15, "after_min": 30, "strength": "hard"}
    for bad in ({"before_min": -1}, {"after_min": 999}, {"before_min": "15"}, {"before_min": True},
                {"before_min": 5, "strength": "maybe"}, "15", [15]):
        with _Raises(ValueError):
            parse_buffer_options(bad)


def test_hard_buffer_vetoes_slots_next_to_an_existing_meeting():
    slots = _slots(_dtm(0, 11), _dtm(0, 11, 15), _dtm(0, 8, 30), _dtm(0, 8), _dtm(1, 11))
    base, _ = cost_vector(slots, BUSY)
    assert None not in base                                    # 여유 조건이 없으면 예전과 같다

    hard = parse_buffer_options({"before_min": 15, "after_min": 15, "strength": "hard"})
    costs, veto = cost_vector(slots, BUSY, hard)
    assert costs[0] is None and veto[0]                        # 11:00~12:00 은 10~11 회의와 붙어 있다
    assert costs[1] is not None                                # 11:15 시작이면 15분이 비어 있다
    assert costs[2] is not None                                # 08:30~09:30 뒤 15분(~09:45)은 10:00 회의와 안 겹친다
    assert costs[3] is not None and costs[4] is not None       # 다른 시간·다른 날은 영향 없음
    # 같은 슬롯이 여유를 늘리면 (30분) 걸린다: 09:30 뒤 30분(~10:00)은 여전히 겹치지 않고,
    # 45분(~10:15)이면 겹친다 (방향별 검증은 아래 테스트)
    wide = parse_buffer_options({"before_min": 0, "after_min": 45, "strength": "hard"})
    assert cost_vector(slots, BUSY, wide)[0][2] is None


def test_buffer_margins_are_directional():
    slots = _slots(_dtm(0, 8, 30), _dtm(0, 11), _dtm(0, 8))
    only_after = parse_buffer_options({"before_min": 0, "after_min": 45, "strength": "hard"})
    costs, _ = cost_vector(slots, BUSY, only_after)
    assert costs[0] is None          # 08:30~09:30 뒤 45분(~10:15)이 10:00 회의와 겹친다
    assert costs[1] is not None      # 11:00 은 회의 '앞' 여유가 0 이라 상관없다
    assert costs[2] is not None      # 08:00~09:00 뒤 45분(~09:45)은 10:00 회의와 안 겹친다
    only_before = parse_buffer_options({"before_min": 45, "after_min": 0, "strength": "hard"})
    costs, _ = cost_vector(slots, BUSY, only_before)
    assert costs[0] is not None and costs[1] is None and costs[2] is not None


def test_soft_buffer_adds_cost_instead_of_a_veto():
    slots = _slots(_dtm(0, 11), _dtm(0, 14))
    plain, _ = cost_vector(slots, BUSY)
    soft, veto = cost_vector(slots, BUSY, parse_buffer_options(
        {"before_min": 30, "after_min": 30, "strength": "soft"}))
    assert not any(veto) and soft[0] == plain[0] + 20.0 and soft[1] == plain[1]


def test_buffer_ignores_movable_events_and_overlap_is_still_a_veto_without_a_buffer():
    busy = [(_dtm(0, 10), _dtm(0, 11), "flex")]
    costs, _ = cost_vector(_slots(_dtm(0, 11)), busy, parse_buffer_options(
        {"before_min": 30, "after_min": 30, "strength": "hard"}))
    assert costs[0] is not None                                # 옮길 수 있는 일정은 여유 판단 대상이 아니다
    costs, _ = cost_vector(_slots(_dtm(0, 10, 30)), BUSY)
    assert costs[0] is None                                    # 겹치면 여전히 거부권


def test_local_agent_widens_its_query_window_by_the_buffer():
    seen = []

    class Spy(FakeCalendar):
        def fetch_events(self, time_min, time_max):
            seen.append((time_min, time_max))
            return super().fetch_events(time_min, time_max)

    prof = _prof()
    agent = LocalAgent(prof, Spy("t", {}), SECRET, GID)
    slots = build_slots(MON, "work", 60)
    agent.open_negotiation("s1", slots)
    agent.open_negotiation("s2", slots, {"before_min": 20, "after_min": 40, "strength": "hard"})
    assert seen[0] == (slots[0][0], slots[-1][1])
    assert seen[1] == (slots[0][0] - dt.timedelta(minutes=20), slots[-1][1] + dt.timedelta(minutes=40))
    with _Raises(ValueError):
        agent.open_negotiation("s3", slots, {"before_min": 5000})


def test_agents_advertise_their_capabilities():
    a = make_agent("민지", organizer=True)
    assert {"buffer", "event_update", "event_cancel", "color_settings"} <= set(a.CAPABILITIES)
    t = InProcessTransport({a.agent_id: a})
    assert t.capabilities(a.agent_id) >= {"buffer"} and t.capabilities("nobody") is None


# ---- 협상 루프 / 재제안 ---------------------------------------------------------------------------

def _session(agents, effects=None, slots=None):
    slots = slots or build_slots(MON, "work", 60)
    transport = InProcessTransport({a.agent_id: a for a in agents})
    sid = new_session_id()
    store.create_session(sid, GID, "work", 60, slots, [
        {"agent_id": a.agent_id, "display_name": a.profile.display_name,
         "email": a.profile.email, "is_organizer": a.profile.is_organizer} for a in agents],
        slot_effects=effects)
    return sid, slots, transport


def _agents():
    return [make_agent("민지", organizer=True), make_agent("준호"), make_agent("서연")]


def test_negotiation_never_proposes_a_hard_excluded_slot():
    agents = _agents()
    slots = build_slots(MON, "work", 60)
    base_sid, _, t = _session(agents, slots=slots)
    first = run_negotiation(base_sid, t, slots, GID).slot_index
    fresh_db()
    agents = _agents()
    sid, slots, t = _session(agents, {"excluded": [first]}, slots)
    out = run_negotiation(sid, t, slots, GID)
    assert out.state == "PENDING_HUMAN_APPROVAL" and out.slot_index != first
    assert first not in {r["slot_index"] for r in store.get_proposals(sid)}


def test_excluding_everything_ends_with_a_condition_specific_reason():
    agents = _agents()
    slots = build_slots(MON, "work", 60)
    sid, slots, t = _session(agents, {"excluded": list(range(len(slots)))}, slots)
    out = run_negotiation(sid, t, slots, GID)
    assert out.state == "NO_COMMON_SLOT" and "회의 조건" in out.reason
    assert store.get_session(sid)["state"] == "NO_COMMON_SLOT"
    assert approval_flow.outcome_for("NO_COMMON_SLOT")["options"]


def test_soft_penalty_picks_an_afternoon_slot_and_only_when_it_is_cheap():
    agents = _agents()
    slots = build_slots(MON, "work", 60)
    plain_sid, _, t = _session(agents, slots=slots)
    plain = run_negotiation(plain_sid, t, slots, GID)
    assert slots[plain.slot_index][0].hour < 12               # 페널티가 없으면 첫 후보(오전)

    fresh_db()
    agents = _agents()
    pen = {str(i): 14.0 for i, (s, e) in enumerate(slots)
           if not (s.hour >= 12 and (e.hour, e.minute) <= (18, 0))}
    sid, slots, t = _session(agents, {"excluded": [], "penalty": pen}, slots)
    out = run_negotiation(sid, t, slots, GID)
    s, e = slots[out.slot_index]
    assert out.state == "PENDING_HUMAN_APPROVAL" and s.hour >= 12 and (e.hour, e.minute) <= (18, 0)
    # 개인 regret 은 페널티와 무관하다 → 잔액에 쓰이는 값에 페널티가 없다
    saved = json.loads(store.get_session(sid)["proposed_regrets"])
    assert saved == {k: float(v) for k, v in out.regrets.items()}


def test_reproposal_after_a_decline_respects_exclusions_and_penalties():
    agents = _agents()
    slots = build_slots(MON, "work", 60)
    probe_sid, _, t = _session(agents, slots=slots)
    probe = run_negotiation(probe_sid, t, slots, GID)
    fresh_db()

    agents = _agents()
    excluded = [probe.slot_index]
    sid, slots, t = _session(agents, {"excluded": excluded}, slots)
    from mediator.notifier import FakeNotifier
    n = FakeNotifier()
    out = run_negotiation(sid, t, slots, GID)
    approval_flow.dispatch_notifications(sid, n)
    seen = [out.slot_index]
    for _ in range(2):
        res = approval_flow.decide_for_agent(sid, agents[1].agent_id, "reject")
        if res.status != "reopened":
            break
        seen.append(store.get_session(sid)["proposed_slot"])
    assert probe.slot_index not in seen and len(set(seen)) == len(seen)


def test_agent_options_reach_only_the_targeted_agents_and_change_only_their_costs():
    slots = build_slots(MON, "work", 60)
    busy = {"primary": [blk(0, 10, 11)]}
    agents = [make_agent("민지", organizer=True), make_agent("준호", busy=busy),
              make_agent("서연", busy=busy)]
    sent = {}
    t = InProcessTransport({a.agent_id: a for a in agents})
    orig = t.open_negotiation

    def spy(agent_id, session_id, slots_, options=None):
        sent[agent_id] = options
        return orig(agent_id, session_id, slots_, options) if options else orig(
            agent_id, session_id, slots_)

    t.open_negotiation = spy
    target = agents[1].agent_id
    effects = {"excluded": [], "penalty": {},
               "agent_options": {target: {"before_min": 30, "after_min": 30, "strength": "hard"}}}
    sid = new_session_id()
    store.create_session(sid, GID, "work", 60, slots, [
        {"agent_id": a.agent_id, "display_name": a.profile.display_name,
         "email": a.profile.email, "is_organizer": a.profile.is_organizer} for a in agents],
        slot_effects=effects)
    out = run_negotiation(sid, t, slots, GID)
    assert sent[target]["before_min"] == 30
    assert sent[agents[0].agent_id] is None and sent[agents[2].agent_id] is None
    # 준호의 캘린더에서 10~11 회의 앞뒤 30분은 후보가 아니다 (09:30~11:30 사이 시작 슬롯)
    regret = store.load_regret(sid)[0]
    i_adj = next(i for i, (s, _) in enumerate(slots) if s == _dtm(0, 11))
    assert regret[target][i_adj] is None and regret[agents[2].agent_id][i_adj] is not None
    assert out.state == "NO_COMMON_SLOT" or out.slot_index != i_adj


def test_session_effects_are_stored_once_and_cannot_be_rewritten():
    sid, slots, t = _session(_agents(), {"excluded": [0, 1], "penalty": {"2": 5.0}})
    row = store.get_session(sid)
    assert store.slot_effects_of(row) == ({0, 1}, {2: 5.0})
    with _Raises(Exception) as exc:
        with store.connect() as c:
            c.execute("UPDATE sessions SET slot_effects_json = '{}' WHERE session_id = ?", (sid,))
    assert "immutable" in str(exc.value)
    assert store.slot_effects_of(store.get_session(sid)) == ({0, 1}, {2: 5.0})


def test_sessions_without_conditions_behave_exactly_as_before():
    sid, slots, t = _session(_agents())
    row = store.get_session(sid)
    assert row["slot_effects_json"] is None and store.slot_effects_of(row) == (set(), {})
    assert store.agent_options_of(row) == {}
    out = run_negotiation(sid, t, slots, GID)
    assert out.state == "PENDING_HUMAN_APPROVAL"
    assert policy_mod.ApprovalPolicy.from_row(store.get_session(sid)).mode == "all"
