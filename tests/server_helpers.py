"""mediator.server 를 FAKE 캘린더 + FakeNotifier 로 돌리는 도우미. 테스트 함수는 여기에 없다.

실제 이메일, 실제 Google Calendar, 실제 LLM 은 쓰지 않는다.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from mediator import approval_flow, change_flow
from mediator import notifier as nm
from mediator.notifier import FakeNotifier
from tests.flow_helpers import fresh_db


class FakeServer:
    """mediator.server 를 FAKE 캘린더 모드로 돌리고, 끝나면 원래대로 돌려놓는다."""

    def __enter__(self):
        from mediator import server
        self.server = server
        self.saved = (server.USE_FAKE, server._transport, dict(server._agent_names),
                      dict(server._cap_cache))
        fresh_db()
        server.USE_FAKE = True
        server._transport = None
        server._agent_names.clear()
        server._cap_cache.clear()
        server._build_transport()
        self.notifier = FakeNotifier()
        nm.set_notifier(self.notifier)
        approval_flow.configure(emit=server._broadcast,
                                on_all_approved=server.schedule_finalize,
                                on_new_round=server.schedule_dispatch)
        change_flow.configure(emit=server._broadcast, on_created=server._change_created,
                              on_accepted=server._change_accepted,
                              on_settled=server._change_settled)
        self.client = TestClient(server.app)
        return self

    def __exit__(self, *a):
        srv = self.server
        srv.join_background()                       # 남은 백그라운드 작업이 다음 테스트의 DB 를 건드리지 않게
        srv.USE_FAKE, srv._transport, names, caps = self.saved
        srv._agent_names.clear()
        srv._agent_names.update(names)
        srv._cap_cache.clear()
        srv._cap_cache.update(caps)
        approval_flow.reset_hooks()
        change_flow.reset_hooks()
        nm.set_notifier(FakeNotifier())

    # ---- 조회 ----
    @property
    def agents(self) -> list:
        return list(self.server._transport._agents.values())

    def agent(self, name: str):
        return next(a for a in self.agents if a.profile.display_name == name)

    def events(self) -> int:
        return sum(len(a.backend.created_events) for a in self.agents)

    def post_link(self, message, verdict):
        return self.client.post("/approve/" + self.notifier.token_of(message),
                                data={"verdict": verdict})

    # ---- 일정 변경: 접수(참가자) → 처리 방법 선택(주최자, v5) ----
    def open_intake_id(self, session_id: str) -> str:
        """이 회의에 열려 있는(아직 주최자가 고르지 않은) 접수의 id."""
        from mediator import change_flow
        cards = change_flow.intake_cards(session_id, only_open=True)
        assert cards, "열린 변경 요청 접수가 없음"
        return cards[0]["intakeId"]

    def decide_change(self, intake_id: str, action: str):
        """주최자가 관리자 API로 접수된 요청의 처리 방법을 고른다."""
        return self.client.post(f"/api/change-requests/{intake_id}/decide",
                                json={"action": action})

    def latest_mail(self, name: str):
        email = self.agent(name).profile.email
        return self.notifier.latest_for(email)

    # ---- 회의 요청: 자연어 → 초안 → 확정 (v5: 확정 전에는 아무것도 시작되지 않는다) ----
    def draft(self, text: str, client=None, key: str | None = None) -> dict:
        import uuid
        r = (client or self.client).post("/api/requests", json={
            "text": text, "idempotencyKey": key or uuid.uuid4().hex})
        assert r.status_code == 200 and r.json()["status"] == "draft", r.text
        return r.json()

    def start_meeting(self, text: str, client=None) -> str:
        """초안을 만들고, 기본값·질문을 확인한 뒤 확정해 회의를 시작한다. 회의 id 를 돌려준다."""
        c = client or self.client
        j = self.draft(text, c)
        did, d = j["draftId"], j["draft"]
        ack = {"ack_defaults": [x["key"] for x in d["defaults"]]}
        for q in d["questions"]:
            if q["pending"]:
                raise AssertionError("질문이 남아 있어 start_meeting 으로 시작할 수 없음: " + q["text"])
        if ack["ack_defaults"]:
            d = c.post(f"/api/requests/{did}/resolve", json=ack).json()["draft"]
        assert d["ready"], d["blocking"]
        r = c.post(f"/api/requests/{did}/confirm")
        assert r.status_code == 200 and r.json()["status"] == "started", r.text
        return r.json()["meetingId"]


def iter_routes(app_or_routes):
    """(경로, 메서드 집합) 을 전부 훑는다. include_router 로 붙은 라우터 안쪽까지 들어간다."""
    routes = getattr(app_or_routes, "routes", app_or_routes)
    for r in routes:
        inner = getattr(r, "original_router", None)
        if inner is not None:
            yield from iter_routes(inner.routes)
        elif getattr(r, "path", None) and getattr(r, "methods", None):
            yield r.path, set(r.methods)