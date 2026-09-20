"""회귀 테스트: 외부 웹에서 "다음 주 월요일부터 수요일 사이에 2시간 워크숍 시간을 찾아줘. 가능하면
오전으로." 를 보내면 화면에 "요청을 처리하지 못했어요." 가 뜨던 문제.

실제 원인은 두 가지였다.

1. `dashboard/app.js` 가 초안 응답의 상태값을 `"needs_input"` 으로 비교했지만, 서버(`mediator/product_api.py`)는
   항상 `"draft"` 를 보낸다. 그래서 성공한 초안도 프론트엔드에서 "인식하지 못한 상태"로 취급되어
   `d.detail` 이 없는 채 일반 오류 문구로 떨어졌다 — 서버는 정상이었고 화면 쪽 계약 불일치였다.
2. `mediator/nl_rules.py` 의 규칙 기반 파서가 "월요일부터 수요일" 같은 요일 범위를 두 요일의
   나열(월, 수)로만 읽어 화요일을 빠뜨렸다 (요일 범위 전용 토큰이 없었음).

이 파일은 실제 API 계약(백엔드가 보내는 값)과 프론트엔드가 확인하는 값이 같은지, 그리고 해당 문장이
실제로 편집 가능한 초안으로 끝나는지를 검증한다. LLM 은 쓰지 않는다(운영 서버도 ANTHROPIC_API_KEY 가
비어 있어 실제로 규칙 기반 경로를 탄다).
"""
import json
import re
import threading
from pathlib import Path

from common import config
from mediator import store
from tests.admin_helpers import AdminLogin
from tests.server_helpers import FakeServer

ROOT = Path(__file__).resolve().parent.parent
WORKSHOP = "다음 주 월요일부터 수요일 사이에 2시간 워크숍 시간을 찾아줘. 가능하면 오전으로."
NAMES = ["재원", "도연", "승현"]


def _count(table):
    with store.connect() as c:
        return c.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


def _req(fx, text, key="workshop-key"):
    return fx.client.post("/api/requests", json={"text": text, "idempotencyKey": key})


# ---- 실제 원인 1: 프론트엔드/백엔드 상태값 계약 ------------------------------------------------------

def test_dashboard_js_checks_the_status_value_the_backend_actually_sends():
    """app.js 는 백엔드가 실제로 보내는 "draft" 를 확인해야 한다.

    이 테스트가 다시 실패한다면, 누군가 프론트엔드의 상태값 이름을 백엔드와 다시 어긋나게 고친 것이다."""
    js = (ROOT / "dashboard" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"function handleRequestResult\(r\)\s*\{.*?\n\}", js, re.S)
    assert m, "handleRequestResult 함수를 찾지 못했습니다"
    body = m.group(0)
    assert 'd.status === "draft"' in body
    assert '"needs_input"' not in body, "프론트엔드가 백엔드에 없는 상태값을 확인하고 있습니다"


def test_backend_never_actually_sends_needs_input():
    """product_api.py 가 보내는 초안 상태값은 항상 'draft' 다 (스키마 CHECK 제약과도 같다)."""
    src = (ROOT / "mediator" / "product_api.py").read_text(encoding="utf-8")
    assert '"needs_input"' not in src
    assert src.count('"status": "draft"') >= 1


# ---- 실제 원인 2 + 전체 흐름: 문제 문장이 실제로 편집 가능한 초안이 되는지 --------------------------------

def test_the_reported_workshop_sentence_becomes_an_editable_draft_not_an_error():
    with FakeServer() as fx:
        r = _req(fx, WORKSHOP)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "draft", body      # 예전엔 프론트엔드가 이걸 "오류"로 오인했다
        d = body["draft"]

        # 소요 시간: '2시간' → 120분
        assert d["summary"]["durationMin"] == 120

        # 날짜 범위: 요청 자체는 '다음 주' 전체를 기본 탐색 기간으로 잡고, 월~수 범위는
        # 사용자가 확인할 수 있는 '선호' 조건으로 나타난다 (draft 화면에서 직접 수정 가능).
        weekday_cons = [c for c in d["constraints"]
                       if c["editable"] and c.get("value") and "weekdays" in (c["value"] or {})]
        assert weekday_cons, d["constraints"]
        wc = weekday_cons[0]
        assert wc["value"]["weekdays"] == [0, 1, 2], "월·화·수가 모두 포함돼야 한다 (화요일 누락 버그)"
        assert wc["strength"] == "선호" and not wc["hard"]

        # 오전 선호: soft, 06:00~12:00
        morning = [c for c in d["constraints"] if c.get("value", {}).get("label") == "오전"]
        assert morning and morning[0]["strength"] == "선호"
        assert morning[0]["value"]["start_time"] == "06:00" and morning[0]["value"]["end_time"] == "12:00"

        # 참가자: 문장에 없으므로 등록된 전체 참가자를 기본값으로 제안하고, 화면에 그렇게 표시된다
        assert d["filled"]["participants"] is True
        assert sorted(d["summary"]["participants"]) == sorted(NAMES)
        assert any(x["key"] == "participants" for x in d["defaults"])
        assert any("참가자" in b["message"] for b in d["blocking"])

        # 확인 전에는 아무 일도 일어나지 않는다
        assert d["ready"] is False                  # 기본값(참가자) 확인이 필요해 아직 확정할 수 없다
        assert _count("sessions") == 0 and not fx.notifier.outbox
        assert d.get("meetingId") is None

        # 내부 식별자가 응답에 노출되지 않는다
        blob = json.dumps(body, ensure_ascii=False)
        for secret in ("grp", "agent_id", "@x.com", "token"):
            assert secret not in blob, secret


def test_confirming_the_workshop_draft_after_review_starts_exactly_one_session():
    with FakeServer() as fx:
        j = _req(fx, WORKSHOP).json()
        did = j["draftId"]
        d = j["draft"]

        # 사용자가 조건을 확인한다 (기본값 확인만 하면 됨 - 지원되지 않는 조건 없음)
        ack = fx.client.post(f"/api/requests/{did}/resolve",
                             json={"ack_defaults": [x["key"] for x in d["defaults"]]}).json()["draft"]
        assert ack["ready"] is True
        assert _count("sessions") == 0                # 확인만으로는 아직 시작하지 않는다

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
        assert len({r["meetingId"] for r in results}) == 1          # 동시 확정도 세션 하나만
        assert _count("sessions") == 1
        sid = results[0]["meetingId"]
        row = store.get_session(sid)
        assert row is not None and row["duration_min"] == 120


def test_two_named_people_still_excludes_the_third_after_the_frontend_fix():
    """목표 3 회귀: '재원, 도연 두 명과만' 은 프론트엔드 계약이 고쳐진 뒤에도 그대로 정확히 동작해야 한다."""
    with FakeServer() as fx:
        r = _req(fx, "재원, 도연 두 명과만 다음 주 오후에 60분 회의를 잡아줘.", key="exact-two")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "draft"
        selected = sorted(x["name"] for x in body["draft"]["people"]["selected"])
        assert selected == ["도연", "재원"]
        # '승현'은 (추가할 수도 있는) available 목록에는 보일 수 있지만, 실제 선택·요약·협상 대상에는 없다
        assert "승현" not in json.dumps(body["draft"]["summary"], ensure_ascii=False)
        assert "승현" not in [x["name"] for x in body["draft"]["people"]["selected"]]


def test_a_request_from_the_public_https_origin_is_accepted_normally():
    """실제 운영처럼 관리자 로그인이 설정된 상태에서, 외부 HTTPS 주소(Cloudflare 터널 등)와 같은
    Origin/Host 로 온 요청은 정상 처리돼야 한다. (Host 가 공개 주소면 로컬 우회 인증은 적용되지 않으므로
    실제 로그인 세션이 있어야 한다 — 이는 운영 서버의 실제 동작과 같다.)"""
    old = config.PUBLIC_BASE_URL
    config.PUBLIC_BASE_URL = "https://demo-tunnel.trycloudflare.com"
    ext_headers = {"Host": "demo-tunnel.trycloudflare.com", "Origin": "https://demo-tunnel.trycloudflare.com"}
    try:
        with FakeServer() as fx:
            with AdminLogin(fx.client) as adm:
                r = fx.client.post("/api/requests", json={"text": WORKSHOP, "idempotencyKey": "ext-origin"},
                                   headers={**ext_headers, "X-CSRF-Token": adm.csrf})
                assert r.status_code == 200 and r.json()["status"] == "draft", r.text
    finally:
        config.PUBLIC_BASE_URL = old


def test_cross_site_origin_is_still_blocked():
    """다른 사이트가 브라우저를 시켜 보낸 것처럼 보이는 요청(Origin 다름)은 이번 수정과 무관하게 그대로 막힌다.

    비로그인 상태에서 관리자 API가 401 인지는 tests/test_admin_auth.py 가 이미 실제 로그인 설정으로 검증한다."""
    with FakeServer() as fx:
        r = fx.client.post("/api/requests", json={"text": WORKSHOP},
                           headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
