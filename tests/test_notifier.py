"""승인 요청 이메일: 내용, 발송 실패/재시도, SMTP(TLS)·비밀번호 비노출.

이 파일의 어떤 테스트도 네트워크에 접속하지 않는다. SMTP 는 가짜 smtplib 로
바꿔 끼우고, 나머지는 FakeNotifier 를 쓴다.
"""
import json
import logging
import os
import smtplib
import types

from common import config
from common.slots import format_slot
from mediator import approval_flow, store
from mediator import notifier as nm
from mediator.notifier import (ConsoleNotifier, FakeNotifier, NotificationError,
                               NotifierConfigError, SmtpNotifier, SmtpSettings)
from tests.flow_helpers import NAMES, email_of, fresh_db, start_flow

PW = "pw-SENTINEL-9f3a-zz"
PENDING = "PENDING_HUMAN_APPROVAL"


def setup_function(fn=None):
    fresh_db()


# ---- 가짜 smtplib -------------------------------------------------------------

def _fake_smtplib(calls, *, starttls=True, login_exc=None, send_exc=None):
    class _Conn:
        def __init__(self, kind, host, port, **kw):
            calls.append((kind, host, port, "context" in kw))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            calls.append(("quit",))

        def ehlo(self):
            calls.append(("ehlo",))

        def has_extn(self, name):
            return starttls and name == "starttls"

        def starttls(self, context=None):
            calls.append(("starttls", context is not None))

        def login(self, user, password):
            calls.append(("login", user, password))
            if login_exc:
                raise login_exc

        def send_message(self, msg):
            calls.append(("send", msg))
            if send_exc:
                raise send_exc

    return types.SimpleNamespace(
        SMTP=lambda host, port, **kw: _Conn("SMTP", host, port, **kw),
        SMTP_SSL=lambda host, port, **kw: _Conn("SMTP_SSL", host, port, **kw),
        SMTPException=smtplib.SMTPException)


class _Patched:
    """notifier 모듈의 smtplib 를 잠시 가짜로, 그룹 비밀키를 개발용이 아닌 값으로 바꾼다."""

    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self.old_smtp, self.old_secret = nm.smtplib, config.GROUP_SECRET
        nm.smtplib = self.fake
        config.GROUP_SECRET = "test-not-a-dev-secret-0123456789"
        return self

    def __exit__(self, *a):
        nm.smtplib, config.GROUP_SECRET = self.old_smtp, self.old_secret


def _settings(port=587):
    return SmtpSettings(host="smtp.example.test", port=port,
                        username="sender@example.test",
                        from_addr="FairMeet <sender@example.test>", password=PW)


def _message():
    f = start_flow()
    return f, f.notifier.outbox[0]


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


# ---- 메일 내용 ----------------------------------------------------------------

def test_email_contains_slot_and_personal_link():
    f = start_flow()
    s, e = f.slots[f.slot_index]
    slot_text = format_slot(s, e)
    assert len(f.notifier.outbox) == 3
    for m in f.notifier.outbox:
        assert slot_text in m.subject and slot_text in m.body_text
        assert m.approval_url in m.body_text and m.approval_url in m.body_html
        assert m.approval_url.startswith(config.PUBLIC_BASE_URL.rstrip("/") + "/approve/")
        assert m.to_name in m.body_text
        assert "까지 유효" in m.body_text and "한 번만" in m.body_text
        assert "이유를 설명할 필요가 없습니다" in m.body_text
    assert len({m.approval_url for m in f.notifier.outbox}) == 3      # 참가자별로 다른 링크


def test_email_never_lists_other_participants_or_their_status():
    f = start_flow()
    for m in f.notifier.outbox:
        others = [n for n in NAMES if n != m.to_name]
        assert not any(o in m.body_text or o in m.body_html for o in others)


# ---- 발송 실패와 재시도 ----------------------------------------------------------

def test_send_failure_is_recorded_and_never_disguised_as_success():
    n = FakeNotifier(fail_for={email_of("준호")}, fail_message="SMTPServerDisconnected: 연결 끊김")
    f = start_flow(notifier=n)

    assert f.session["state"] == PENDING                       # 재시도 가능한 상태로 남는다
    rows = {r["agent_id"]: r for r in store.get_notifications(f.sid, 1)}
    failed = rows[f.agent_id("준호")]
    assert failed["status"] == "failed" and "연결 끊김" in failed["last_error"]
    assert {rows[f.agent_id(x)]["status"] for x in ("민지", "서연")} == {"sent"}
    assert f.token_for("준호") is None                         # 준호는 링크를 받지 못했다

    view = approval_flow.session_view(f.sid)
    assert view["notification"]["status"] == "partial_failure"
    assert view["notification"]["retryable"] is True
    by = {p["name"]: p for p in view["participants"]}
    assert by["준호"]["label"] == "발송 실패" and "연결 끊김" in by["준호"]["error"]
    assert by["민지"]["label"] == "발송됨" and by["민지"]["error"] is None

    # 두 명이 승인해도 링크를 못 받은 사람이 있으면 확정되지 않는다
    assert f.approve("민지").status == "recorded"
    assert f.approve("서연").status == "recorded"
    assert f.session["state"] == PENDING and f.created_events() == 0


def test_retry_resends_only_failed_participants_with_a_fresh_link():
    n = FakeNotifier(fail_for={email_of("준호")})
    f = start_flow(notifier=n)
    assert len(n.outbox) == 2

    again = f.dispatch()                                       # 여전히 실패 — 예외 없이 기록만
    assert (again.sent, again.failed) == (0, 1)
    row = {r["agent_id"]: r for r in store.get_notifications(f.sid, 1)}[f.agent_id("준호")]
    assert row["status"] == "failed" and row["attempts"] == 2

    n.fail_for.clear()
    ok = f.dispatch()
    assert (ok.sent, ok.failed) == (1, 0)
    assert len(n.outbox) == 3                                  # 이미 보낸 두 명에게는 재발송 없음
    assert [m.to_email for m in n.outbox].count(email_of("민지")) == 1

    row = {r["agent_id"]: r for r in store.get_notifications(f.sid, 1)}[f.agent_id("준호")]
    assert row["status"] == "sent" and row["last_error"] is None and row["attempts"] == 3
    with store.connect() as c:
        tok = c.execute("SELECT revoked_at, used_at FROM approval_tokens WHERE "
                        "session_id=? AND agent_id=? ORDER BY created_at",
                        (f.sid, f.agent_id("준호"))).fetchall()
    assert len(tok) == 3 and all(t["revoked_at"] for t in tok[:-1]) and not tok[-1]["revoked_at"]

    assert approval_flow.inspect_token(f.token_for("준호")) is not None
    assert [f.approve(x).status for x in NAMES] == ["recorded", "recorded", "all_approved"]
    assert approval_flow.session_view(f.sid)["notification"]["status"] in ("ok", "none")


def test_all_email_failed_status_and_no_progress():
    n = FakeNotifier()
    n.fail_all = True
    f = start_flow(notifier=n)
    v = approval_flow.session_view(f.sid)
    assert v["notification"]["status"] == "all_failed" and v["notification"]["failed"] == 3
    assert n.outbox == [] and f.created_events() == 0
    assert all(abs(x) < 1e-9 for x in f.balances().values())


def test_misconfigured_backend_becomes_send_failure_not_a_crash():
    f = start_flow()                                            # 정상 1순위 (Fake)
    f.reject("준호")                                             # 새 제안 → 아직 발송 전
    old_build = nm.build_notifier

    def broken(*a, **k):
        raise NotifierConfigError("SMTP 설정이 비어 있습니다: FAIRMEET_SMTP_HOST")
    nm.build_notifier = broken
    nm.set_notifier(None)
    try:
        summary = approval_flow.dispatch_notifications(f.sid)   # notifier 를 안 넘김
    finally:
        nm.build_notifier = old_build
        nm.set_notifier(FakeNotifier())
    assert summary.failed == 3 and summary.sent == 0
    assert "FAIRMEET_SMTP_HOST" in summary.errors[0][1]
    assert f.session["state"] == PENDING


# ---- SMTP: TLS, 자격증명, 비밀번호 비노출 -----------------------------------------------

def test_smtp_uses_starttls_before_login_and_sends_the_message():
    f, m = _message()
    calls = []
    with _Patched(_fake_smtplib(calls)):
        SmtpNotifier(_settings(587)).send(m)

    kinds = [c[0] for c in calls]
    assert kinds == ["SMTP", "ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert calls[0][1:3] == ("smtp.example.test", 587)
    assert calls[2] == ("starttls", True)                       # 검증 컨텍스트로 TLS 시작
    assert calls[4][1:] == ("sender@example.test", PW)
    msg = calls[5][1]
    assert msg["From"] == "FairMeet <sender@example.test>"
    assert m.to_name in str(msg["To"]) and m.to_email in str(msg["To"])
    assert m.approval_url in msg.get_body(("plain",)).get_content()
    assert msg.get_body(("html",)) is not None


def test_smtp_465_uses_implicit_ssl():
    _, m = _message()
    calls = []
    with _Patched(_fake_smtplib(calls)):
        SmtpNotifier(_settings(465)).send(m)
    assert calls[0][0] == "SMTP_SSL" and calls[0][3] is True    # context 전달
    assert "starttls" not in [c[0] for c in calls]
    assert "login" in [c[0] for c in calls]


def test_smtp_refuses_to_send_credentials_without_tls():
    _, m = _message()
    calls = []
    with _Patched(_fake_smtplib(calls, starttls=False)):
        try:
            SmtpNotifier(_settings(587)).send(m)
        except NotificationError as exc:
            assert "STARTTLS" in str(exc)
        else:
            raise AssertionError("TLS 없이 발송이 진행됨")
    assert "login" not in [c[0] for c in calls] and "send" not in [c[0] for c in calls]


def test_smtp_error_text_never_contains_the_password():
    _, m = _message()
    exc = smtplib.SMTPAuthenticationError(535, f"bad credentials for {PW}".encode())
    with _Patched(_fake_smtplib([], login_exc=exc)):
        try:
            SmtpNotifier(_settings()).send(m)
        except NotificationError as got:
            assert PW not in str(got) and "SMTPAuthenticationError" in str(got)
            assert got.__cause__ is None and got.__suppress_context__
        else:
            raise AssertionError("실패가 전파되지 않음")


def test_password_never_reaches_logs_database_or_dashboard():
    cap = _LogCapture()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    os.environ["FAIRMEET_SMTP_PASSWORD"] = PW
    exc = smtplib.SMTPException(f"550 rejected, auth was {PW}")
    try:
        with _Patched(_fake_smtplib([], send_exc=exc)):
            f = start_flow(notifier=SmtpNotifier(_settings()))
            # 라이브러리 밖에서 새는 경우(2차 방어)도 확인한다
            leaky = FakeNotifier(fail_message=f"boom {PW}")
            leaky.fail_all = True
            g = start_flow(notifier=leaky)
        with store.connect() as c:
            dump = repr([tuple(r) for t in ("notifications", "transitions", "sessions",
                                            "approval_tokens", "approvals")
                         for r in c.execute(f"SELECT * FROM {t}")])
        views = json.dumps([approval_flow.session_view(f.sid),
                            approval_flow.session_view(g.sid)], ensure_ascii=False)
    finally:
        root.removeHandler(cap)
        root.setLevel(old_level)
        os.environ["FAIRMEET_SMTP_PASSWORD"] = ""

    assert f.session["state"] == PENDING
    assert "SMTPException" in views                              # 오류 메시지는 관리자에게 보인다
    assert PW not in dump and PW not in views
    assert not any(PW in line for line in cap.lines)
    assert PW not in repr(_settings())
    with _Patched(_fake_smtplib([])):
        assert PW not in repr(SmtpNotifier(_settings()))


def test_smtp_settings_report_only_variable_names_and_normalize_password():
    try:
        SmtpSettings.from_env({"FAIRMEET_SMTP_PASSWORD": PW})
    except NotifierConfigError as exc:
        text = str(exc)
        assert "FAIRMEET_SMTP_HOST" in text and "FAIRMEET_SMTP_USERNAME" in text
        assert PW not in text
    else:
        raise AssertionError("빈 설정이 통과됨")

    s = SmtpSettings.from_env({
        "FAIRMEET_SMTP_HOST": "smtp.gmail.com", "FAIRMEET_SMTP_USERNAME": "me@gmail.com",
        "FAIRMEET_SMTP_PASSWORD": "abcd efgh ijkl mnop", "FAIRMEET_SMTP_FROM": "me@gmail.com"})
    assert s.port == 587 and s.password == "abcdefghijklmnop"
    assert "abcd" not in repr(s)


def test_real_sending_is_blocked_while_using_the_public_dev_secret():
    old = config.GROUP_SECRET
    config.GROUP_SECRET = config._DEV_FALLBACK["FAIRMEET_GROUP_SECRET"]
    try:
        SmtpNotifier(_settings())
    except NotifierConfigError as exc:
        assert "FAIRMEET_GROUP_SECRET" in str(exc)
    else:
        raise AssertionError("서명 키가 공개된 기본값인데 SMTP 가 허용됨")
    finally:
        config.GROUP_SECRET = old


def test_header_injection_is_rejected():
    with _Patched(_fake_smtplib([])):
        _, m = _message()
        bad = nm.ApprovalMessage(**{**m.__dict__, "to_name": "x\r\nBcc: evil@example.test"})
        try:
            SmtpNotifier(_settings()).send(bad)
        except NotificationError:
            return
    raise AssertionError("헤더 인젝션이 통과됨")


def test_console_backend_never_touches_the_network():
    class _Boom:
        def __getattr__(self, name):
            raise AssertionError("console 백엔드가 smtplib 를 사용함")

    _, m = _message()
    old = nm.smtplib
    nm.smtplib = _Boom()
    try:
        ConsoleNotifier().send(m)
    finally:
        nm.smtplib = old


# ---- 선택 / 설정 경고 -------------------------------------------------------------

def test_backend_selection_and_public_url_warnings():
    assert isinstance(nm.build_notifier("console"), ConsoleNotifier)
    assert isinstance(nm.build_notifier("fake"), FakeNotifier)
    try:
        nm.build_notifier("carrier-pigeon")
    except NotifierConfigError:
        pass
    else:
        raise AssertionError("알 수 없는 백엔드가 통과됨")

    assert any("localhost" in w for w in nm.base_url_warnings("http://127.0.0.1:8000", "smtp"))
    assert any("HTTPS" in w for w in nm.base_url_warnings("http://example.com", "smtp"))
    assert nm.base_url_warnings("https://fairmeet.example.com", "smtp") == []
    assert any("실제 이메일은 발송되지 않습니다" in w
               for w in nm.base_url_warnings("https://x.example.com", "console"))


def test_test_suite_is_isolated_from_real_smtp():
    """tests/__init__.py 가 .env 의 SMTP 값이 테스트 프로세스에 들어오지 못하게 막는다."""
    assert config.EMAIL_BACKEND == "fake"
    for name in ("HOST", "PORT", "USERNAME", "PASSWORD", "FROM"):
        assert os.environ.get(f"FAIRMEET_SMTP_{name}") in ("", None)
