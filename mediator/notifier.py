"""승인 요청 알림 (이메일).

`ApprovalNotifier` 는 교체 가능한 발송 인터페이스다. 협상/승인 로직은
이 인터페이스만 알고, 실제로 SMTP 로 나가는지 콘솔에 찍히는지 모른다.

  SmtpNotifier     실제 발송. TLS 필수 (587 STARTTLS 또는 465 SSL). 평문 전송 없음.
  ConsoleNotifier  개발용. 메일을 보내지 않고 로그에 찍는다.
                   ⚠ 승인 링크가 로그에 남으므로 운영에서는 쓰지 않는다.
  FakeNotifier     테스트용. 메모리에 쌓기만 한다. 실패 주입 가능.

SMTP 비밀번호는 발송 시점에 환경변수에서 읽고, 로그·예외 메시지·repr 어디에도
남기지 않는다. 라이브러리가 낸 예외 메시지에 섞여 나올 가능성까지 고려해
관리자에게 보이는 오류 문자열은 반드시 redact() 를 거친다.
"""
from __future__ import annotations

import datetime as dt
import html
import logging
import os
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import urlparse

from common import config
from common.slots import KST

log = logging.getLogger("fairmeet.notifier")


class NotificationError(RuntimeError):
    """발송 실패. 메시지는 관리자 화면에 그대로 표시되므로 비밀값이 없어야 한다."""


class NotifierConfigError(NotificationError):
    """발송 설정이 없거나 잘못됨. 값이 아니라 환경변수 '이름'만 언급한다."""


@dataclass(frozen=True)
class ApprovalMessage:
    to_email: str
    to_name: str
    subject: str
    body_text: str
    body_html: str
    approval_url: str
    session_id: str
    proposal_version: int
    agent_id: str


def redact(text: str, *secrets: str) -> str:
    """text 안의 비밀값을 지운다. 비어 있거나 너무 짧은 값은 무시한다."""
    out = str(text)
    for s in secrets:
        if not s or len(s) < 4:
            continue
        for variant in {s, "".join(s.split())}:
            if variant:
                out = out.replace(variant, "***")
    return out


# ---- 메시지 생성 --------------------------------------------------------------

def approval_url(token: str, base_url: str | None = None) -> str:
    base = (base_url or config.PUBLIC_BASE_URL).rstrip("/")
    return f"{base}/approve/{token}"


def render_approval_message(*, to_email: str, to_name: str, slot_text: str,
                            url: str, expires_at: int, session_id: str,
                            proposal_version: int, agent_id: str,
                            policy_text: str | None = None,
                            role_text: str | None = None,
                            change_note: str | None = None) -> ApprovalMessage:
    """제안 시간과 개인 링크가 담긴 승인 요청 메일을 만든다.

    policy_text / role_text 는 전원 승인이 아닌 회의에서만 들어온다. 필수 참석 여부와
    확정 기준을 받는 사람이 알고 응답하게 하려는 것이다 (거절자 정보는 담기지 않는다).
    """
    until = dt.datetime.fromtimestamp(expires_at, KST)
    until_text = f"{until:%m월 %d일 %H:%M} (KST)"
    replace_note = ("이 요청은 앞서 안내드린 제안을 대체합니다. "
                    "이전 링크는 더 이상 사용할 수 없습니다.\n"
                    ) if proposal_version > 1 else ""
    policy_lines = "".join(f"- {t}\n" for t in (role_text, policy_text, change_note) if t)

    subject = f"[FairMeet] 회의 시간 승인 요청 — {slot_text}"
    text = (
        f"{to_name} 님, 안녕하세요.\n\n"
        f"FairMeet 에이전트가 아래 시간을 제안했습니다.\n\n"
        f"    {slot_text}\n\n"
        f"아래 링크에서 동의 또는 거절을 선택해 주세요.\n"
        f"거절하셔도 이유를 설명할 필요가 없습니다.\n\n"
        f"    {url}\n\n"
        f"{policy_lines}"
        f"- 이 링크는 {until_text} 까지 유효하며, 한 번만 사용할 수 있습니다.\n"
        f"- {to_name} 님 전용 링크입니다. 다른 사람에게 전달하지 마세요.\n"
        f"- 다른 참가자의 응답 여부는 표시되지 않습니다.\n"
        f"{('- ' + replace_note) if replace_note else ''}"
    )
    esc = html.escape
    policy_html = "".join(f"<li>{esc(t)}</li>" for t in (role_text, policy_text, change_note) if t)
    body_html = (
        f"<p>{esc(to_name)} 님, 안녕하세요.</p>"
        f"<p>FairMeet 에이전트가 아래 시간을 제안했습니다.</p>"
        f"<p style=\"font-size:1.25em;font-weight:600\">{esc(slot_text)}</p>"
        f"<p><a href=\"{esc(url, quote=True)}\">동의 또는 거절하기</a><br>"
        f"거절하셔도 이유를 설명할 필요가 없습니다.</p>"
        f"<ul>{policy_html}"
        f"<li>이 링크는 {esc(until_text)} 까지 유효하며, 한 번만 사용할 수 있습니다.</li>"
        f"<li>{esc(to_name)} 님 전용 링크입니다. 다른 사람에게 전달하지 마세요.</li>"
        f"<li>다른 참가자의 응답 여부는 표시되지 않습니다.</li>"
        f"{('<li>' + esc(replace_note.strip()) + '</li>') if replace_note else ''}"
        f"</ul>")
    return ApprovalMessage(
        to_email=to_email, to_name=to_name, subject=subject, body_text=text,
        body_html=body_html, approval_url=url, session_id=session_id,
        proposal_version=proposal_version, agent_id=agent_id)


def change_url(purpose: str, token: str, base_url: str | None = None) -> str:
    """일정 변경 링크. purpose: 'request'(변경 요청) | 'vote'(변경 투표)"""
    base = (base_url or config.PUBLIC_BASE_URL).rstrip("/")
    return f"{base}/change/{purpose}/{token}"


def render_change_message(*, to_email: str, to_name: str, subject: str, lines: list[str],
                          session_id: str, agent_id: str, url: str | None = None,
                          link_label: str = "", expires_at: int | None = None,
                          footer: list[str] | None = None) -> ApprovalMessage:
    """확정 안내 / 변경 투표 요청 / 변경 결과 메일을 만든다.

    요청자의 신원·사유는 어떤 메일에도 담지 않는다. 링크가 있으면 개인 전용·일회용이라고 알린다.
    """
    esc = html.escape
    foot = list(footer or [])
    if url and expires_at:
        until = dt.datetime.fromtimestamp(expires_at, KST)
        foot += [f"이 링크는 {until:%m월 %d일 %H:%M} (KST) 까지 유효하며, 한 번만 사용할 수 있습니다.",
                 f"{to_name} 님 전용 링크입니다. 다른 사람에게 전달하지 마세요."]
    text = f"{to_name} 님, 안녕하세요.\n\n" + "\n".join(lines) + "\n\n"
    if url:
        text += f"    {url}\n\n"
    text += "".join(f"- {f}\n" for f in foot)
    body_html = f"<p>{esc(to_name)} 님, 안녕하세요.</p>" + "".join(
        f"<p>{esc(l)}</p>" for l in lines)
    if url:
        body_html += f"<p><a href=\"{esc(url, quote=True)}\">{esc(link_label or '열기')}</a></p>"
    if foot:
        body_html += "<ul>" + "".join(f"<li>{esc(f)}</li>" for f in foot) + "</ul>"
    return ApprovalMessage(
        to_email=to_email, to_name=to_name, subject=subject, body_text=text,
        body_html=body_html, approval_url=url or "", session_id=session_id,
        proposal_version=0, agent_id=agent_id)


# ---- 인터페이스 ---------------------------------------------------------------

class ApprovalNotifier(ABC):
    """승인 요청 한 통을 보낸다."""

    name = "abstract"

    @abstractmethod
    def send(self, message: ApprovalMessage) -> None:
        """성공하면 아무것도 반환하지 않고, 실패하면 NotificationError 를 던진다."""


class ConsoleNotifier(ApprovalNotifier):
    """개발용. 실제로 발송하지 않는다."""

    name = "console"

    def send(self, message: ApprovalMessage) -> None:
        log.info("[console 메일 — 실제 발송 아님] to=%s subject=%s url=%s",
                 message.to_email, message.subject, message.approval_url)


class FakeNotifier(ApprovalNotifier):
    """테스트용. 보낸 메일을 outbox 에 쌓고, 지정한 수신자에게는 실패시킨다."""

    name = "fake"

    def __init__(self, fail_for: set[str] | None = None,
                 fail_message: str = "SMTPServerDisconnected: 테스트용 발송 실패"):
        self.outbox: list[ApprovalMessage] = []
        self.attempts: list[str] = []
        self.fail_for: set[str] = set(fail_for or ())
        self.fail_all = False
        self.fail_message = fail_message

    def send(self, message: ApprovalMessage) -> None:
        self.attempts.append(message.to_email)
        if self.fail_all or message.to_email in self.fail_for:
            raise NotificationError(self.fail_message)
        self.outbox.append(message)

    # 테스트 편의
    def latest_for(self, email: str) -> ApprovalMessage | None:
        for m in reversed(self.outbox):
            if m.to_email == email:
                return m
        return None

    def token_of(self, message: ApprovalMessage) -> str:
        return message.approval_url.rsplit("/approve/", 1)[1]

    def link_token(self, message: ApprovalMessage) -> str:
        """승인이 아닌 링크(일정 변경 요청/투표)의 토큰."""
        return message.approval_url.rsplit("/", 1)[1]

    def mails(self, subject_contains: str = "", email: str | None = None) -> list:
        return [m for m in self.outbox if subject_contains in m.subject
                and (email is None or m.to_email == email)]


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    from_addr: str
    password: str = field(repr=False, default="")

    @classmethod
    def from_env(cls, env=None) -> "SmtpSettings":
        env = os.environ if env is None else env
        names = {"host": "FAIRMEET_SMTP_HOST", "port": "FAIRMEET_SMTP_PORT",
                 "username": "FAIRMEET_SMTP_USERNAME",
                 "password": "FAIRMEET_SMTP_PASSWORD",
                 "from_addr": "FAIRMEET_SMTP_FROM"}
        raw = {k: (env.get(v) or "").strip() for k, v in names.items()}
        if not raw["port"]:
            raw["port"] = "587"
        missing = [names[k] for k in ("host", "username", "password", "from_addr")
                   if not raw[k]]
        if missing:
            raise NotifierConfigError(
                "SMTP 설정이 비어 있습니다: " + ", ".join(missing)
                + " (.env 를 확인하세요)")
        try:
            port = int(raw["port"])
        except ValueError:
            raise NotifierConfigError("FAIRMEET_SMTP_PORT 는 숫자여야 합니다") from None
        # 구글 앱 비밀번호는 4자리씩 공백으로 나뉘어 표시된다. 공백은 무시된다.
        password = "".join(raw["password"].split())
        return cls(host=raw["host"], port=port, username=raw["username"],
                   from_addr=raw["from_addr"], password=password)


class SmtpNotifier(ApprovalNotifier):
    """TLS 로만 발송한다. 465 는 처음부터 SSL, 그 외 포트는 STARTTLS 를 강제한다."""

    name = "smtp"
    TIMEOUT_SEC = 20

    def __init__(self, settings: SmtpSettings):
        if config.using_dev_secrets():
            # 서명 키가 공개된 기본값이면 승인 링크를 누구나 위조할 수 있다.
            raise NotifierConfigError(
                "FAIRMEET_GROUP_SECRET 이 개발용 기본값입니다. "
                "실제 이메일을 보내기 전에 .env 에 새 값을 설정하세요.")
        self._s = settings

    def __repr__(self) -> str:
        return f"<SmtpNotifier {self._s.host}:{self._s.port}>"

    def _build(self, m: ApprovalMessage) -> EmailMessage:
        msg = EmailMessage()
        try:
            msg["Subject"] = m.subject
            msg["From"] = self._s.from_addr
            msg["To"] = formataddr((m.to_name, m.to_email))
        except ValueError as exc:                      # 헤더 인젝션 등
            raise NotificationError(f"메일 헤더가 올바르지 않습니다: {exc}") from None
        msg.set_content(m.body_text)
        msg.add_alternative(m.body_html, subtype="html")
        return msg

    def send(self, message: ApprovalMessage) -> None:
        s = self._s
        msg = self._build(message)
        ctx = ssl.create_default_context()
        secrets = (s.password,)
        try:
            if s.port == 465:
                server = smtplib.SMTP_SSL(s.host, s.port,
                                          timeout=self.TIMEOUT_SEC, context=ctx)
            else:
                server = smtplib.SMTP(s.host, s.port, timeout=self.TIMEOUT_SEC)
            with server:
                if s.port != 465:
                    server.ehlo()
                    if not server.has_extn("starttls"):
                        raise NotificationError(
                            "SMTP 서버가 STARTTLS 를 지원하지 않아 "
                            "평문 전송을 하지 않고 중단했습니다")
                    server.starttls(context=ctx)
                    server.ehlo()
                server.login(s.username, s.password)
                server.send_message(msg)
        except NotificationError:
            raise
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            # from None: 원 예외를 체인으로 남기지 않는다.
            raise NotificationError(
                redact(f"{type(exc).__name__}: {exc}", *secrets)[:300]) from None


# ---- 선택/주입 ----------------------------------------------------------------

_notifier: ApprovalNotifier | None = None


def build_notifier(backend: str | None = None) -> ApprovalNotifier:
    backend = (backend or config.EMAIL_BACKEND or "console").lower()
    if backend == "smtp":
        return SmtpNotifier(SmtpSettings.from_env())
    if backend == "console":
        return ConsoleNotifier()
    if backend == "fake":
        return FakeNotifier()
    raise NotifierConfigError(
        f"알 수 없는 FAIRMEET_EMAIL_BACKEND: {backend!r} (smtp | console | fake)")


def get_notifier() -> ApprovalNotifier:
    """주입된 notifier 를 쓰고, 없으면 설정으로 만든다.

    SMTP 설정이 틀려도 서버가 죽지 않도록 여기서만 예외가 난다.
    호출자(dispatch)가 이를 '발송 실패'로 기록한다.
    """
    global _notifier
    if _notifier is None:
        _notifier = build_notifier()
    return _notifier


def set_notifier(notifier: ApprovalNotifier | None) -> None:
    global _notifier
    _notifier = notifier


def base_url_warnings(base_url: str | None = None,
                      backend: str | None = None) -> list[str]:
    """관리자 화면에 보여줄 설정 경고. 값이 아니라 상황만 설명한다."""
    base_url = base_url or config.PUBLIC_BASE_URL
    backend = (backend or config.EMAIL_BACKEND or "console").lower()
    warns: list[str] = []
    if backend != "smtp":
        warns.append(f"이메일 백엔드가 '{backend}' 입니다. 실제 이메일은 발송되지 않습니다.")
        return warns
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        warns.append("FAIRMEET_PUBLIC_BASE_URL 이 localhost 입니다. "
                     "받는 사람의 기기에서는 링크가 열리지 않습니다.")
    elif parsed.scheme != "https":
        warns.append("FAIRMEET_PUBLIC_BASE_URL 이 HTTPS 가 아닙니다. "
                     "공개 주소에는 HTTPS 를 사용하세요.")
    return warns
