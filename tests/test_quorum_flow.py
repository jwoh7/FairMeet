"""필수 참석자·승인 정족수가 실제 승인 흐름(이메일 링크 → 판정 → 재제안 → 커밋)에서 동작하는지.

전원 승인이 기본값이며, 정책이 없는 기존 세션은 예전과 똑같이 동작해야 한다.
실제 이메일·Google Calendar 는 쓰지 않는다 (FakeNotifier, FakeCalendar).
"""
import sqlite3

from mediator import approval_flow, store
from mediator import commit as commit_mod
from tests.flow_helpers import NAMES, fresh_db, make_agent, start_flow

PENDING = "PENDING_HUMAN_APPROVAL"


def setup_function(fn=None):
    fresh_db()


def _quorum(n=2, required=()):
    return {"mode": "min_count", "min_count": n, "required": list(required)}


# ---- 기존 동작 보존 -----------------------------------------------------------------

def test_default_session_is_all_approve_and_unchanged():
    f = start_flow()
    row = f.session
    assert row["approval_policy_json"] == '{"mode":"all"}' and row["approvals_needed"] is None
    assert f.approve("민지").status == "recorded"
    assert f.approve("준호").status == "recorded"          # 2/3 으로는 확정되지 않는다
    assert f.session["state"] == PENDING
    assert f.approve("서연").status == "all_approved"
    assert commit_mod.finalize(f.sid, f.transport).state == "COMMITTED"


def test_default_session_emails_have_no_policy_lines():
    f = start_flow()
    body = f.notifier.outbox[0].body_text
    assert "필수 참석자" not in body and "선택 참석자" not in body


# ---- 정족수 확정 ---------------------------------------------------------------------

def test_quorum_confirms_without_waiting_for_the_optional_participant():
    f = start_flow(policy=_quorum(2))
    assert f.session["approvals_needed"] == 2
    assert f.approve("민지").status == "recorded"           # 주최자(자동 필수)
    assert f.approve("준호").status == "all_approved"       # 2명 → 서연은 아직 응답 안 함
    assert commit_mod.finalize(f.sid, f.transport).state == "COMMITTED"
    assert f.created_events() == 1

    # 뒤늦게 서연이 링크를 눌러도 이미 끝난 요청이다 (링크가 폐기됨)
    assert f.approve("서연").status == "invalid"
    assert store.get_verdicts(f.sid) == ({f.agent_id("민지"), f.agent_id("준호")}, set())


def test_optional_participant_can_decline_and_the_meeting_still_confirms():
    f = start_flow(policy=_quorum(2))
    assert f.reject("서연").status == "recorded"            # 기준에 영향 없음 → 재제안 없음
    assert f.version == 1 and f.session["state"] == PENDING
    assert f.approve("민지").status == "recorded"
    assert f.approve("준호").status == "all_approved"
    res = commit_mod.finalize(f.sid, f.transport)
    assert res.state == "COMMITTED" and f.created_events() == 1
    assert len(store.get_proposals(f.sid)) == 1


def test_everyone_is_still_invited_so_a_decline_is_not_revealed_by_the_invite_list():
    f = start_flow(policy=_quorum(2))
    f.reject("서연")
    f.approve("민지")
    f.approve("준호")
    commit_mod.finalize(f.sid, f.transport)
    ev = list(f.agents[0].backend.created_events.values())[0]
    assert sorted(ev["attendees"]) == sorted([f.agents[1].profile.email,
                                              f.agents[2].profile.email])
    assert "승인 기준" in ev["description"] and "2명" in ev["description"]
    assert "서연" not in ev["description"] and "준호" not in ev["description"]


def test_calendar_recheck_ignores_people_who_did_not_approve():
    """동의하지 않은 선택 참석자의 새 일정이 확정을 막으면 안 된다 (전원 승인과 다른 점)."""
    f = start_flow(policy=_quorum(2))
    f.reject("서연")
    f.approve("민지")
    f.approve("준호")
    s, e = f.slots[f.slot_index]
    f.agents[2].backend.add_busy("primary", s, e)           # 서연에게 충돌이 생겼다
    assert commit_mod.finalize(f.sid, f.transport).state == "COMMITTED"

    # 반대로 동의한 사람에게 충돌이 생기면 여전히 막는다
    fresh_db()
    g = start_flow(policy=_quorum(2))
    g.approve("민지")
    g.approve("준호")
    s, e = g.slots[g.slot_index]
    g.agents[1].backend.add_busy("primary", s, e)
    assert commit_mod.finalize(g.sid, g.transport).state == "STALE_CONFLICT"
    assert g.created_events() == 0


# ---- 필수 참석자 / 거절 → 차순위 ------------------------------------------------------

def test_required_participant_decline_moves_to_the_next_candidate():
    f = start_flow(policy=_quorum(2, required=["서연"]))
    first = f.slot_index
    f.approve("민지")
    f.approve("준호")                                       # 수는 충족했지만 서연(필수)이 아직
    assert f.session["state"] == PENDING and f.version == 1
    res = f.reject_and_notify("서연")
    assert res.status == "reopened" and f.version == 2
    assert f.slot_index != first
    assert [p["status"] for p in store.get_proposals(f.sid)] == ["rejected", "open"]
    # 새 제안에는 새 승인이 필요하다 (이전 응답은 재사용되지 않는다)
    assert f.approve("민지").status == "recorded"
    assert f.approve("서연").status == "all_approved"       # 필수 2명 + 총 2명 → 기준 충족
    assert f.approve("준호").status == "invalid"


def test_organizer_decline_always_fails_the_slot():
    f = start_flow(policy=_quorum(2))
    res = f.reject_and_notify("민지")
    assert res.status == "reopened" and f.version == 2


def test_impossible_quorum_moves_on_before_everyone_has_answered():
    agents = [make_agent(n, organizer=(i == 0)) for i, n in
              enumerate(["민지", "준호", "서연", "지훈"])]
    f = start_flow(agents=agents, policy=_quorum(3))
    assert f.reject("준호").status == "recorded"            # 서연·지훈이 승인하면 아직 3명
    res = f.reject_and_notify("서연")                       # 최대 2명 → 지훈 응답 전에 실패
    assert res.status == "reopened" and f.version == 2
    assert store.get_proposals(f.sid)[0]["status"] == "rejected"


def test_declined_slot_is_never_proposed_again_under_quorum():
    f = start_flow(policy=_quorum(2))
    seen = {f.slot_index}
    for _ in range(2):
        res = f.reject_and_notify("민지")
        if res.status != "reopened":
            break
        assert f.slot_index not in seen
        seen.add(f.slot_index)
    assert len(store.get_attempted_slots(f.sid)) == len(seen)


# ---- 비율 정책 ---------------------------------------------------------------------

def test_ratio_policy_behaves_like_the_derived_count():
    f = start_flow(policy={"mode": "min_ratio", "min_ratio": 0.5, "required": []})
    assert f.session["approvals_needed"] == 2               # ceil(0.5 * 3)
    f.approve("민지")
    assert f.approve("서연").status == "all_approved"


# ---- 정책은 바뀌지 않는다 ----------------------------------------------------------------

def test_policy_cannot_be_changed_after_the_session_is_created():
    f = start_flow(policy=_quorum(2))
    before = dict(f.session)
    for sql in ("UPDATE sessions SET approvals_needed = 1 WHERE session_id = ?",
                "UPDATE sessions SET approval_policy_json = '{\"mode\":\"all\"}' WHERE session_id = ?",
                "UPDATE sessions SET required_agents_json = '[]' WHERE session_id = ?",
                "UPDATE sessions SET constraints_json = '{}' WHERE session_id = ?",
                "UPDATE sessions SET slot_effects_json = '{}' WHERE session_id = ?"):
        try:
            with store.connect() as c:
                c.execute(sql, (f.sid,))
        except sqlite3.Error as exc:
            assert "immutable" in str(exc)
        else:
            raise AssertionError(f"정책/조건이 몰래 바뀌었다: {sql}")
    assert dict(f.session) == before

    # 다른 컬럼은 평소처럼 갱신된다
    with store.connect() as c:
        c.execute("UPDATE sessions SET fail_reason = 'x' WHERE session_id = ?", (f.sid,))
    assert f.session["fail_reason"] == "x"


def test_finalize_refuses_to_commit_when_the_policy_is_not_satisfied():
    f = start_flow(policy=_quorum(2))
    f.approve("준호")                                       # 필수(주최자)가 아직 → 기준 미충족
    res = commit_mod.finalize(f.sid, f.transport)
    assert res.state == PENDING and "승인 기준" in res.reason
    assert f.created_events() == 0


# ---- 이메일·화면 ---------------------------------------------------------------------

def test_emails_state_required_or_optional_and_the_rule():
    f = start_flow(policy=_quorum(2, required=["서연"]))
    by_name = {n: f.notifier.latest_for(f.agents[i].profile.email).body_text
               for i, n in enumerate(NAMES)}
    assert "필수 참석자입니다" in by_name["민지"]            # 주최자는 자동 필수
    assert "필수 참석자입니다" in by_name["서연"]
    assert "선택 참석자입니다" in by_name["준호"]
    for body in by_name.values():
        assert "2명 이상" in body and "선택 참석자가 거절해도" in body
    # 응답 상황(누가 승인/거절했는지)은 어떤 메일에도 없다
    f.reject("준호")
    f.approve("민지")
    assert all("거절했" not in m.body_text and "승인했" not in m.body_text
               for m in f.notifier.outbox)


def test_dashboard_view_shows_policy_but_not_who_responded_how():
    f = start_flow(policy=_quorum(2, required=["서연"]))
    view = approval_flow.session_view(f.sid)
    pol = view["policy"]
    assert pol["mode"] == "min_count" and pol["needed"] == 2 and pol["total"] == 3
    assert set(pol["required"]) == {"민지", "서연"}
    f.reject("준호")
    f.approve("민지")
    txt = str(approval_flow.session_view(f.sid))
    assert "reject" not in txt and "approve" not in txt
    legacy = approval_flow.session_view(start_flow().sid)
    assert legacy["policy"]["mode"] == "all" and legacy["policy"]["required"] == []


def test_approval_page_shows_role_and_rule_only_for_quorum_meetings():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from mediator.approval_web import router

    app = FastAPI()
    app.include_router(router)
    c = TestClient(app)

    f = start_flow(policy=_quorum(2, required=["서연"]))
    page = c.get("/approve/" + f.token_for("준호")).text
    assert "선택 참석자입니다" in page and "2명 이상" in page
    page = c.get("/approve/" + f.token_for("서연")).text
    assert "필수 참석자입니다" in page

    r = c.post("/approve/" + f.token_for("준호"), data={"verdict": "reject"})
    assert "다른 참가자의 응답으로 확정 여부가 정해집니다" in r.text
    assert f.session["state"] == PENDING and f.version == 1
    r = c.post("/approve/" + f.token_for("민지"), data={"verdict": "approve"})
    assert "동의가 기록되었습니다" in r.text
    r = c.post("/approve/" + f.token_for("서연"), data={"verdict": "approve"})
    assert "확정 기준을 충족했습니다" in r.text

    fresh_db()
    g = start_flow()
    assert "필수 참석자" not in c.get("/approve/" + g.token_for("민지")).text
    for n in NAMES[:2]:
        c.post("/approve/" + g.token_for(n), data={"verdict": "approve"})
    r = c.post("/approve/" + g.token_for("서연"), data={"verdict": "approve"})
    assert "전원 동의 완료" in r.text
