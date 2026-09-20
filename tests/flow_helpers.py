"""승인 재협상 테스트가 공유하는 도우미. 테스트 함수는 여기에 없다.

실제 이메일도, 실제 Google Calendar 도 쓰지 않는다:
  - 이메일  → FakeNotifier (메모리 outbox)
  - 캘린더  → FakeCalendar + InProcessTransport
"""
from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

from agent.calendar_client import FakeCalendar
from agent.calendar_settings import MemorySettings
from agent.negotiator import LocalAgent
from agent.profile import Profile
from common import config
from common.ids import new_session_id
from common.slots import KST, build_slots
from mediator import approval_flow, store
from mediator import llm as llm_mod
from mediator import notifier as notifier_mod
from mediator import policy as policy_mod
from mediator.negotiation import run_negotiation
from mediator.notifier import FakeNotifier
from mediator.transport import InProcessTransport

MON = dt.date(2026, 9, 21)
GID = "test-flow"
SECRET = "test-secret"
NAMES = ["민지", "준호", "서연"]
_SLUG = {"민지": "minji", "준호": "junho", "서연": "seoyeon", "지훈": "jihun"}

_tmp_db: str | None = None


def email_of(name: str) -> str:
    """테스트용 수신 주소. 실제 주소처럼 ASCII 여야 SMTP 헤더 인코딩이 통과한다."""
    return f"{_SLUG.get(name, 'user')}@x.com"


def fresh_db() -> str:
    """새 임시 DB 를 만들고 스토어가 그것을 쓰게 한다. 실제 fairmeet.db 는 건드리지 않는다."""
    global _tmp_db
    if _tmp_db and Path(_tmp_db).exists():
        try:
            Path(_tmp_db).unlink()
        except OSError:
            pass
    _tmp_db = tempfile.mktemp(suffix=".db")
    store.set_db_path(_tmp_db)
    store.init_db()
    approval_flow.reset_hooks()
    from mediator import change_flow
    change_flow.reset_hooks()                     # 서버 모듈이 import 되어 있어도 백그라운드 작업이 돌지 않게
    notifier_mod.set_notifier(FakeNotifier())     # 실수로 실제 백엔드가 만들어지지 않게
    llm_mod.set_llm(llm_mod.DISABLED)             # 실제 LLM 클라이언트가 만들어지지 않게
    return _tmp_db


def make_agent(name, organizer=False, busy=None, flex_colors=False, events=None):
    """busy: {캘린더 ID: [[시작, 끝], ...]} (옛 형식, 색상 없는 바쁨 = 고정). 캘린더 ID "flexible" 은 색상 5.
    flex_colors=True 면 색상 5 를 유동으로 설정한다. events: 새 형식 일정 목록."""
    p = Profile(user_key=f"{name}@x.com", display_name=name, email=email_of(name),
                is_organizer=organizer)
    return LocalAgent(p, FakeCalendar(name, busy or {}, events=events), SECRET, GID,
                      settings_store=MemorySettings({"5": "flex"}) if flex_colors else None)


def blk(day, h0, h1):
    d = MON + dt.timedelta(days=day)
    return [dt.datetime.combine(d, dt.time(h0), tzinfo=KST).isoformat(),
            dt.datetime.combine(d, dt.time(h1), tzinfo=KST).isoformat()]


class Flow:
    """협상 → 1순위 제안 → 이메일 발송까지 끝낸 세션."""

    def __init__(self, agents, slots, sid, transport, notifier, outcome):
        self.agents = agents
        self.slots = slots
        self.sid = sid
        self.transport = transport
        self.notifier = notifier
        self.outcome = outcome

    # ---- 조회 ----
    @property
    def session(self):
        return store.get_session(self.sid)

    @property
    def version(self) -> int:
        return self.session["proposal_version"]

    @property
    def slot_index(self) -> int:
        return self.session["proposed_slot"]

    def agent_id(self, name) -> str:
        return next(a.agent_id for a in self.agents if a.profile.display_name == name)

    def created_events(self) -> int:
        return sum(len(a.backend.created_events) for a in self.agents)

    def balances(self) -> dict:
        return store.get_balance(GID, [a.agent_id for a in self.agents])

    def token_for(self, name, version=None) -> str | None:
        """가장 최근에 그 참가자에게 발송된(해당 제안 버전의) 링크 토큰."""
        for m in reversed(self.notifier.outbox):
            if m.to_email == email_of(name) and (version is None
                                                  or m.proposal_version == version):
                return self.notifier.token_of(m)
        return None

    # ---- 동작 ----
    def dispatch(self):
        return approval_flow.dispatch_notifications(self.sid, self.notifier)

    def approve(self, name, token=None):
        return approval_flow.decide_with_token(token or self.token_for(name), "approve")

    def reject(self, name, token=None):
        return approval_flow.decide_with_token(token or self.token_for(name), "reject")

    def reject_and_notify(self, name="준호"):
        """거절 → (새 제안이 열렸으면) 새 이메일 발송."""
        res = self.reject(name)
        if res.status == "reopened":
            self.dispatch()
        return res


def build_test_policy(agents, spec):
    """spec = {"mode": ..., "min_count": .., "min_ratio": .., "required": [이름, ...]}"""
    ids = {a.profile.display_name: a.agent_id for a in agents}
    organizer = next((a.agent_id for a in agents if a.profile.is_organizer), None)
    return policy_mod.build_policy(
        spec["mode"], min_count=spec.get("min_count"), min_ratio=spec.get("min_ratio"),
        required=[ids[n] for n in spec.get("required", [])],
        participant_ids=list(ids.values()), organizer_id=organizer)


def start_flow(n=3, nslots=None, agents=None, notifier=None, slots=None,
               policy=None) -> Flow:
    """n명의 에이전트로 협상해 1순위 제안을 열고 승인 요청 이메일을 발송한다.

    policy: 승인 정책 명세 dict (build_test_policy 참고). 없으면 전원 승인.
    """
    if agents is None:
        agents = [make_agent(nm, organizer=(i == 0)) for i, nm in enumerate(NAMES[:n])]
    slots = slots or build_slots(MON, "work", 60)
    if nslots:
        slots = slots[:nslots]
    transport = InProcessTransport({a.agent_id: a for a in agents})
    sid = new_session_id()
    store.create_session(sid, GID, "work", 60, slots, [
        {"agent_id": a.agent_id, "display_name": a.profile.display_name,
         "email": a.profile.email, "is_organizer": a.profile.is_organizer}
        for a in agents], policy=build_test_policy(agents, policy) if policy else None)
    out = run_negotiation(sid, transport, slots, GID, lam=config.LAMBDA_WORK)
    notifier = notifier or FakeNotifier()
    flow = Flow(agents, slots, sid, transport, notifier, out)
    if out.state == "PENDING_HUMAN_APPROVAL":
        flow.dispatch()
    return flow


class MaxRounds:
    """FAIRMEET_MAX_APPROVAL_ROUNDS 를 테스트 동안만 바꾼다."""

    def __init__(self, value: int):
        self.value = value

    def __enter__(self):
        self.old = config.MAX_APPROVAL_ROUNDS
        config.MAX_APPROVAL_ROUNDS = self.value
        return self

    def __exit__(self, *exc):
        config.MAX_APPROVAL_ROUNDS = self.old
