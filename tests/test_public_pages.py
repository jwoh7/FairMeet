"""화면 파일과 공개(참가자) 페이지의 정적·구조 검사: XSS 방지, 외부 자산 없음, 모바일 대응, 폴링 우선 구조, TDS 비사용.

브라우저 없이 확인할 수 있는 것만 여기서 확인한다. 실제 렌더링은 헤드리스 브라우저로 따로 확인했다(README 참고).
"""
import re
import uuid
from pathlib import Path

from common import config
from mediator import auth, store
from tests.server_helpers import FakeServer

ROOT = Path(__file__).resolve().parent.parent
DASH = ROOT / "dashboard"


def _read(name):
    return (DASH / name).read_text(encoding="utf-8")


def setup_function(fn=None):
    auth.reset_state()


# ---- 화면 코드의 안전성 --------------------------------------------------------------------------------

def test_the_dashboard_never_builds_html_from_strings():
    js = _read("app.js")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                   "setAttribute(\"style\"", "srcdoc", "javascript:"):
        assert banned not in js, banned
    html = _read("index.html")
    assert "<style" not in html and " style=" not in html and "onclick=" not in html   # CSP: 인라인 스타일/핸들러 없음
    assert html.count("<script") == 1 and 'src="/assets/app.js"' in html


def test_only_display_preferences_are_stored_in_the_browser():
    js = _read("app.js")
    assert "sessionStorage" not in js and "document.cookie" not in js and "indexedDB" not in js
    assert js.count("localStorage.setItem") == 1 and 'PREF_KEY = "fm.pref"' in js
    for call in re.findall(r"savePref\(\{([^}]*)\}\)", js):
        assert call.strip() == "filter: k", call                          # 화면 필터만 저장한다
    lowered = js.lower()
    assert "localstorage" in lowered and not re.search(r"localstorage[^;\n]*(csrf|token|password|secret)", lowered)


def test_no_external_resources_or_third_party_design_assets_are_used():
    for name in ("index.html", "app.js", "fairmeet.css"):
        text = _read(name)
        urls = re.findall(r"https?://[^\s\"')]+", text)
        assert set(urls) <= {"http://www.w3.org/2000/svg"}, (name, urls)
        assert "@import" not in text and "cdn." not in text.lower()
        assert not re.search(r"toss|tds[-_ ]|@toss|apps-in-toss", text, re.IGNORECASE), name
    assert not list(DASH.glob("*.woff*")) and not list(DASH.glob("*.png")) and not list(DASH.glob("*.svg"))
    assert "자체 제작" in _read("fairmeet.css")


def test_there_is_no_pwa_service_worker_or_mobile_only_navigation():
    files = {p.name for p in DASH.iterdir()}
    assert files == {"index.html", "app.js", "fairmeet.css"}
    html, js = _read("index.html"), _read("app.js")
    assert "manifest" not in html and "serviceWorker" not in js and "apple-touch" not in html
    assert "bottom-nav" not in _read("fairmeet.css") and "position:fixed" not in _read("fairmeet.css").replace(
        ".toast{position:fixed", "")


def test_the_dashboard_updates_by_polling_and_does_not_depend_on_sse():
    js = _read("app.js")
    assert "EventSource" not in js and "/events" not in js
    assert "schedulePoll" in js and "setTimeout" in js and "visibilitychange" in js


def test_a_failed_or_unavailable_event_stream_does_not_affect_the_polled_state():
    with FakeServer() as fx:
        assert fx.client.get("/events").status_code == 403                    # 시연 모드가 아니면 SSE 자체가 닫혀 있다
        j = {"meetingId": fx.start_meeting("다음 주 수요일 오후에 재원, 도연, 승현과 60분 회의를 잡아줘.")}
        first = fx.client.get(f"/api/meetings/{j['meetingId']}").json()
        second = fx.client.get(f"/api/meetings/{j['meetingId']}").json()
        assert first == second and first["headline"] and first["progress"]["total"] == 3      # 다시 받아도 중복 없이 같은 상태


def test_login_and_keyboard_accessibility_hooks_exist_in_the_ui():
    js, css = _read("app.js"), _read("fairmeet.css")
    for needle in ("aria-live", 'aria-current', "aria-label", "role: \"alert\"", "isComposing", "focus("):
        assert needle in js or needle in _read("index.html"), needle
    assert ":focus-visible" in css and "prefers-reduced-motion" in css and "prefers-color-scheme: dark" in css
    assert "본문으로 건너뛰기" in _read("index.html")
    assert "STEP_MARK" in js and "sr-only" in js                              # 색만으로 상태를 구분하지 않는다


# ---- 참가자 페이지 (이메일 링크로 여는 화면) ------------------------------------------------------------------

def test_participant_pages_share_one_mobile_friendly_frame_without_inline_styles():
    with FakeServer() as fx:
        j = {"meetingId": fx.start_meeting("다음 주 수요일 오후에 재원, 도연, 승현과 60분 회의를 잡아줘.")}
        mail = next(m for m in fx.notifier.outbox if m.session_id == j["meetingId"])
        page = fx.client.get("/approve/" + fx.notifier.token_of(mail))
        html = page.text
        assert page.status_code == 200 and 'name="viewport" content="width=device-width,initial-scale=1"' in html
        assert 'href="/assets/fairmeet.css"' in html and "<style" not in html and " style=" not in html
        assert 'class="participant"' in html and "noindex" in html and page.headers["x-robots-tag"] == "noindex"
        assert page.headers["cache-control"] == "no-store" and page.headers["referrer-policy"] == "no-referrer"
        assert "시연" not in html and "demo" not in html.lower() and "app.js" not in html   # 관리자 화면 요소가 없다
        stale = fx.client.get("/approve/not-a-token")
        assert "만료되었거나 이미 처리된 요청입니다" in stale.text and stale.text.count("<button") == 0


def test_participant_css_keeps_buttons_large_and_the_layout_narrow():
    css = _read("fairmeet.css")
    m = re.search(r"body\.participant \.actions button\{[^}]*min-height:([\d.]+)rem", css)
    assert m and float(m.group(1)) >= 3.0                                     # 손가락으로 누르기 쉬운 크기
    assert re.search(r"body\.participant \.wrap\{max-width:3\d(\.\d+)?rem", css)
    assert "flex-wrap:wrap" in re.search(r"body\.participant \.actions\{[^}]*\}", css).group(0)
    assert "@media (max-width:900px)" in css                                  # 좁은 화면에서 겹치지 않는 기본 반응형


def test_change_and_material_pages_have_no_inline_styles_either():
    for name in ("change_web.py", "material_web.py", "approval_web.py"):
        src = (ROOT / "mediator" / name).read_text(encoding="utf-8")
        assert 'style="' not in src and "<style" not in src, name


def test_public_screens_carry_no_secrets_or_internal_words():
    with FakeServer() as fx:
        blobs = [fx.client.get("/").text, fx.client.get("/assets/app.js").text, fx.client.get("/assets/fairmeet.css").text,
                 fx.client.get("/approve/x").text, fx.client.get("/materials/x").text]
        for b in blobs:
            for secret in (config.GROUP_SECRET, config.AGENT_TOKEN):
                assert secret not in b
            assert "GROUP_SECRET" not in b and "Traceback" not in b and "sqlite" not in b.lower()
        with store.connect() as c:
            assert c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"] == 0        # 화면을 열기만 해서는 아무것도 생기지 않는다
