"""GoogleCalendar.fetch_busy 의 오류 처리 회귀 테스트 (네트워크·Google 계정 없이).

버그: 오래 유휴였던 에이전트의 첫 FreeBusy 호출이 끊긴 연결 때문에 HttpError 가 아닌
예외(ConnectionResetError, 인증 갱신 오류 등)를 내면 그대로 전파되어, 에이전트가 원인
불명의 HTTP 500 을 돌려주고 중재자는 'agent 응답 없음'으로만 보였다. 이제 재시도를
요청하고, 그래도 실패하면 CalendarUnavailable(503, fail-closed)로 명확히 알린다.
"""
import datetime as dt

from agent.calendar_client import (FREEBUSY_RETRIES, CalendarUnavailable,
                                   GoogleCalendar)
from common.slots import KST

T0 = dt.datetime(2026, 9, 21, 9, tzinfo=KST)
T1 = dt.datetime(2026, 9, 21, 18, tzinfo=KST)


class _Query:
    def __init__(self, outcome, seen):
        self.outcome, self.seen = outcome, seen

    def execute(self, num_retries=0):
        self.seen.append(num_retries)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Service:
    def __init__(self, outcome):
        self.outcome, self.seen = outcome, []

    def freebusy(self):
        return self

    def query(self, body):
        return _Query(self.outcome, self.seen)


def _cal(outcome):
    cal = GoogleCalendar("tokens/does-not-exist.json")
    cal._service = _Service(outcome)
    return cal


def test_transient_errors_are_requested_to_be_retried():
    cal = _cal({"calendars": {"primary": {"busy": []}}})
    assert cal.fetch_busy(["primary"], T0, T1) == {"primary": []}
    assert cal._service.seen == [FREEBUSY_RETRIES] and FREEBUSY_RETRIES >= 1


def test_connection_error_becomes_calendar_unavailable_not_a_raw_exception():
    for exc in (ConnectionResetError("[WinError 10054] 현재 연결은 원격 호스트에 의해 강제로 끊겼습니다"),
                TimeoutError("timed out"), RuntimeError("boom")):
        cal = _cal(exc)
        try:
            cal.fetch_busy(["primary"], T0, T1)
        except CalendarUnavailable as got:
            assert type(exc).__name__ in str(got)               # 원인이 메시지에 남는다
        else:
            raise AssertionError(f"{type(exc).__name__} 이 CalendarUnavailable 로 바뀌지 않음")
        assert cal._service is None                             # 다음 호출은 새 연결로 시작


def test_auth_refresh_failure_is_reported_as_calendar_unavailable():
    from google.auth.exceptions import RefreshError
    cal = _cal(RefreshError("invalid_grant: Token has been expired or revoked."))
    try:
        cal.fetch_busy(["primary"], T0, T1)
    except CalendarUnavailable as got:
        assert "RefreshError" in str(got)
    else:
        raise AssertionError("인증 갱신 실패가 그대로 전파됨")


def test_calendar_level_errors_stay_fail_closed():
    cal = _cal({"calendars": {"primary": {"errors": [{"reason": "notFound"}]}}})
    try:
        cal.fetch_busy(["primary"], T0, T1)
    except CalendarUnavailable:
        return
    raise AssertionError("캘린더 오류를 '한가함'으로 처리함")
