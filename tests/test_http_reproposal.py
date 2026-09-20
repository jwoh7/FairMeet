"""실제 HTTP transport 로 '거절 → 차순위 재제안 → 전원 승인 → 커밋'을 끝까지 돌린다.

tests/test_http_commit.py (기존 회귀 테스트)는 그대로 두고, 같은 방식으로 에이전트
서버 3개를 서브프로세스로 띄운다. 캘린더는 FakeCalendar(FAIRMEET_FAKE=1)라서
Google 계정 없이 결정론적이고, 이메일도 FakeNotifier 라서 아무것도 발송되지 않는다.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from agent.profile import Profile
from common import config
from common.ids import group_pseudonym, new_session_id
from common.slots import build_slots, next_monday
from mediator import approval_flow, store
from mediator import commit as commit_mod
from mediator.negotiation import run_negotiation
from mediator.notifier import FakeNotifier
from mediator.transport import HttpTransport
from tests.flow_helpers import fresh_db
from tests.test_http_commit import (GROUP_ID, KEYS, ROOT, _free_port,
                                    _spawn_agent, _wait_healthy)


def setup_function(fn=None):
    fresh_db()


def test_http_transport_reject_then_reproposal_reaches_committed():
    ports = {k: _free_port() for k in KEYS}
    procs = [_spawn_agent(k, ports[k]) for k in KEYS]
    try:
        for k in KEYS:
            _wait_healthy(ports[k])

        endpoints, ids, profiles = {}, {}, {}
        for k in KEYS:
            prof = Profile.load(Path(ROOT) / "profiles" / f"{k}.json")
            aid = group_pseudonym(config.GROUP_SECRET, GROUP_ID, prof.user_key)
            endpoints[aid], ids[k], profiles[k] = f"http://127.0.0.1:{ports[k]}", aid, prof

        transport = HttpTransport(endpoints, config.AGENT_TOKEN)
        slots = build_slots(next_monday(), "work", 60)
        sid = new_session_id()
        store.create_session(sid, GROUP_ID, "work", 60, slots, [
            {"agent_id": ids[k], "display_name": profiles[k].display_name,
             "email": profiles[k].email, "is_organizer": profiles[k].is_organizer}
            for k in KEYS])

        outcome = run_negotiation(sid, transport, slots, GROUP_ID)
        assert outcome.state == "PENDING_HUMAN_APPROVAL", (outcome.state, outcome.reason)
        first = outcome.slot_index

        notifier = FakeNotifier()
        assert approval_flow.dispatch_notifications(sid, notifier).sent == 3

        # 한 명이 거절 → 같은 세션에서 다음 후보로 재제안
        rejecter = notifier.outbox[0]
        res = approval_flow.decide_with_token(notifier.token_of(rejecter), "reject")
        assert res.status == "reopened"
        second = store.get_session(sid)["proposed_slot"]
        assert second != first
        assert approval_flow.dispatch_notifications(sid, notifier).sent == 3
        assert len(notifier.outbox) == 6

        # 이전 링크는 죽고, 새 링크로 전원이 다시 승인해야 한다
        assert approval_flow.decide_with_token(
            notifier.token_of(notifier.outbox[1]), "approve").status == "invalid"
        results = [approval_flow.decide_with_token(notifier.token_of(m), "approve").status
                   for m in notifier.outbox[3:]]
        assert results == ["recorded", "recorded", "all_approved"]

        # 실제 HTTP 로 재확인 → 커밋
        done = commit_mod.finalize(sid, transport)
        assert done.state == "COMMITTED", (done.state, done.reason)
        assert done.newly_created is True and done.event_id
        row = store.get_session(sid)
        assert row["google_event_id"] == done.event_id and row["balance_applied"] == 1
        assert row["proposed_slot"] == second
        assert [p["status"] for p in store.get_proposals(sid)] == ["rejected", "approved"]
        assert len(store.get_history(GROUP_ID)) == 3

        again = commit_mod.finalize(sid, transport)                 # 중복 호출 → 두 번째 이벤트 없음
        assert again.state == "COMMITTED"
        assert store.get_session(sid)["google_event_id"] == done.event_id
    finally:
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
