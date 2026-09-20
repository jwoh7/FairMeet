"""회귀 테스트: 초안 화면에서 '기본값(참가자 등록된 전체) 확인만 남았는데도 이대로 요청하기 버튼이
계속 비활성화돼 눌러도 아무 반응이 없던 문제.

실제 원인: `dashboard/app.js`의 `draftCard()`가 버튼을 `disabled: !d.ready` 로만 계산했고,
`d.defaults`(기본값으로 채운 항목)를 사용자가 확인할 수 있는 UI(버튼·체크박스 등)를 전혀 그리지
않았다. 즉 '기본값만 남은' 상태에서는 확인할 방법 자체가 화면에 없어 버튼이 영원히 막혀 있었다.

수정: 실제로 답해야 할 질문·충돌·미지원 조건이 없고 '기본값 확인'만 남았을 때는 버튼을 막지 않고,
누르면 화면에 보이는 기본값을 확인(ack_defaults)한 뒤 곧바로 확정(confirm)한다 — 사용자 관점에서는
버튼 한 번이다. 이 파일은 프론트엔드가 의존하는 API 순서(ack_defaults → confirm)가 실제로 그렇게
동작하는지, 그리고 app.js 가 실제로 그 계약을 지키는 코드로 고쳐졌는지를 검증한다.

실제 fairmeet.db·SMTP·Google Calendar 는 쓰지 않는다 (FakeServer: 임시 DB + FakeNotifier + FakeCalendar).
"""
import json
import threading
import uuid
from pathlib import Path

from mediator import store
from tests.server_helpers import FakeServer

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")

# 참가자를 말하지 않아 '참가자(등록된 전체)' 기본값만 남고, 다른 조건은 모두 명확한 문장.
DEFAULTS_ONLY = "다음 주 수요일 오후에 60분 회의를 잡아줘."


def _count(table):
    with store.connect() as c:
        return c.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


def _req(fx, text, key=None):
    return fx.client.post("/api/requests", json={"text": text, "idempotencyKey": key or uuid.uuid4().hex})


# ---- app.js 계약: 프론트엔드가 실제로 고쳐진 로직을 쓰는지 --------------------------------------------

def test_the_button_is_not_disabled_just_because_defaults_are_unconfirmed():
    assert "onlyDefaultsPending" in JS, "기본값만 남은 경우를 판정하는 로직이 없습니다"
    assert "d.defaults.length > 0 && d.blocking.length === 1" in JS


def test_confirm_draft_acknowledges_defaults_before_confirming():
    assert "ack_defaults" in JS
    m = JS.find("async function confirmDraft")
    assert m != -1, "confirmDraft 함수를 찾지 못했습니다"
    body = JS[m:JS.find("\nasync function ", m + 10)]
    assert "/resolve" in body and "/confirm" in body
    assert body.index("/resolve") < body.index("/confirm"), "확인(resolve)이 확정(confirm)보다 먼저 와야 합니다"


def test_confirm_draft_guards_against_double_clicks():
    m = JS.find("async function confirmDraft")
    body = JS[m:JS.find("\nasync function ", m + 10)]
    assert "S.confirming" in body and "if (S.confirming" in body


def test_reload_restores_the_current_draft_from_the_server():
    assert "/api/requests/current" in JS, "새로고침 복원 API를 프론트엔드가 호출하지 않습니다"


# ---- 실제 API 순서(ack_defaults → confirm)가 정말 그렇게 동작하는지 ------------------------------------

def test_ack_defaults_then_confirm_in_one_flow_creates_exactly_one_session():
    with FakeServer() as fx:
        j = _req(fx, DEFAULTS_ONLY).json()
        assert j["status"] == "draft"
        d = j["draft"]
        assert d["ready"] is False
        assert [x["key"] for x in d["defaults"]] == ["participants"]
        assert len(d["blocking"]) == 1                      # 실제 차단 사유가 기본값 하나뿐

        did = j["draftId"]
        ack = fx.client.post(f"/api/requests/{did}/resolve",
                             json={"ack_defaults": [x["key"] for x in d["defaults"]]}).json()
        assert ack["draft"]["ready"] is True and _count("sessions") == 0     # 확인만으로는 시작하지 않는다

        r = fx.client.post(f"/api/requests/{did}/confirm")
        assert r.status_code == 200 and r.json()["status"] == "started"
        assert _count("sessions") == 1
        row = store.get_session(r.json()["meetingId"])
        assert sorted(json.loads(row["required_agents_json"])) == []        # 필수 지정 없음(기본값 참가자 전원)


def test_rapid_double_confirm_after_ack_creates_one_session_and_one_mail_set():
    with FakeServer() as fx:
        j = _req(fx, DEFAULTS_ONLY).json()
        did = j["draftId"]
        fx.client.post(f"/api/requests/{did}/resolve",
                       json={"ack_defaults": [x["key"] for x in j["draft"]["defaults"]]})

        results, barrier = [], threading.Barrier(2)

        def go():
            barrier.wait()
            results.append(fx.client.post(f"/api/requests/{did}/confirm").json())

        threads = [threading.Thread(target=go) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(r["status"] == "started" for r in results)
        assert len({r["meetingId"] for r in results}) == 1
        assert _count("sessions") == 1
        mails = [m for m in fx.notifier.outbox if m.session_id == results[0]["meetingId"]
                and "/approve/" in m.approval_url]
        assert len(mails) == 3                               # 메일 세트도 한 번만


def test_a_failed_confirm_leaves_the_draft_open_for_a_retry():
    with FakeServer() as fx:
        srv = fx.server
        real = srv._start_negotiation
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("injected failure")
            return real(*a, **k)

        srv._start_negotiation = flaky
        try:
            j = _req(fx, DEFAULTS_ONLY).json()
            did = j["draftId"]
            fx.client.post(f"/api/requests/{did}/resolve",
                           json={"ack_defaults": [x["key"] for x in j["draft"]["defaults"]]})
            from fastapi.testclient import TestClient
            quiet = TestClient(srv.app, raise_server_exceptions=False)
            bad = quiet.post(f"/api/requests/{did}/confirm")
            assert bad.status_code == 500 and _count("sessions") == 0
            # 실패 후에도 같은 초안으로 다시 확정할 수 있다 (프론트엔드는 버튼을 다시 활성화한다)
            again = fx.client.post(f"/api/requests/{did}/confirm")
            assert again.status_code == 200 and again.json()["status"] == "started"
            assert _count("sessions") == 1
        finally:
            srv._start_negotiation = real


def test_real_pending_questions_still_block_confirmation_even_after_ack():
    """모호한 질문이 남아 있으면 기본값을 확인해도 시작되지 않는다."""
    with FakeServer() as fx:
        text = "다음 주 화요일부터 목요일 사이에 90분 회의를 잡아줘. 재원은 필수이고 최소 2명이 동의하면 돼."
        j = _req(fx, text).json()
        d = j["draft"]
        assert d["ready"] is False
        pending = [q for q in d["questions"] if q["pending"]]
        assert pending                                        # 실제로 답해야 할 질문이 있다
        assert len(d["blocking"]) > (1 if d["defaults"] else 0)   # 기본값 하나만이 아니다

        did = j["draftId"]
        if d["defaults"]:
            fx.client.post(f"/api/requests/{did}/resolve",
                           json={"ack_defaults": [x["key"] for x in d["defaults"]]})
        r = fx.client.post(f"/api/requests/{did}/confirm")
        assert r.status_code == 409 and _count("sessions") == 0
