"""에이전트 접근 계층.

같은 협상 코드가 두 경로에서 돌아간다:
  InProcessTransport — 한 프로세스 안. 데모·테스트용. 네트워크 없음.
  HttpTransport      — 실제 분리된 에이전트 서버.

전송 계층을 주입 가능하게 만든 덕분에, 전체 협상 흐름을 fastapi 없이도
끝까지 테스트할 수 있다.
"""
from __future__ import annotations

import datetime as dt
from typing import Protocol

from agent.calendar_client import CalendarUnavailable, EventNotFound
from agent.calendar_settings import SettingsError


class AgentUnreachable(RuntimeError):
    """에이전트 응답 없음. 빈 시간으로 처리하지 않고 사람에게 넘긴다."""


class AgentHttpError(AgentUnreachable):
    """에이전트가 200 이 아닌 응답을 돌려줌. status 로 원인을 구분할 수 있다."""

    def __init__(self, message: str, status: int, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class Transport(Protocol):
    def agent_ids(self) -> list[str]: ...
    def open_negotiation(self, agent_id: str, session_id: str, slots,
                         options: dict | None = None) -> dict: ...
    def respond(self, agent_id: str, session_id: str, round_no: int,
                slot_index: int, balance_hint: float) -> dict: ...

    def recheck(self, agent_id: str, start: dt.datetime, end: dt.datetime) -> bool:
        """커밋 직전 재확인. True 면 충돌이 있다."""
        ...

    def commit(self, agent_id: str, *, event_id: str, summary: str,
               start: dt.datetime, end: dt.datetime,
               attendee_emails: list[str], description: str = ""
               ) -> tuple[str, bool]:
        """organizer 의 에이전트에게만 호출한다."""
        ...

    def capabilities(self, agent_id: str) -> set[str] | None:
        """에이전트가 지원하는 선택 기능. 확인할 수 없으면 None (지원한다고 가정하지 않는다)."""
        ...

    def update_event(self, agent_id: str, *, event_id: str,
                     start: dt.datetime | None = None, end: dt.datetime | None = None,
                     attendee_emails: list[str] | None = None) -> bool:
        """확정된 이벤트를 고친다. 이벤트가 없으면 EventNotFound."""
        ...

    def cancel_event(self, agent_id: str, event_id: str) -> bool:
        """확정된 이벤트를 취소한다. 이미 없으면 False."""
        ...

    def color_settings(self, agent_id: str) -> dict:
        """참가자의 일정 색상 설정 화면 데이터 (팔레트·종류·색상별 개수). 일정 내용은 없다."""
        ...

    def save_color_settings(self, agent_id: str, pairs: list,
                            expected_version: int | None = None) -> dict:
        """[{colorId, kind}] 저장. 잘못되면 SettingsError."""
        ...


class InProcessTransport:
    """LocalAgent 객체를 직접 호출한다."""

    def __init__(self, agents: dict):
        """agents: {agent_id: LocalAgent}"""
        self._agents = agents

    def agent_ids(self) -> list[str]:
        return sorted(self._agents)

    def agent(self, agent_id: str):
        return self._agents[agent_id]

    def open_negotiation(self, agent_id, session_id, slots, options=None) -> dict:
        try:
            if options:
                return self._agents[agent_id].open_negotiation(session_id, slots, options)
            return self._agents[agent_id].open_negotiation(session_id, slots)
        except CalendarUnavailable:
            raise
        except KeyError as exc:
            raise AgentUnreachable(f"에이전트 {agent_id} 없음") from exc

    def respond(self, agent_id, session_id, round_no, slot_index, balance_hint) -> dict:
        try:
            return self._agents[agent_id].respond(
                session_id, round_no, slot_index, balance_hint)
        except KeyError as exc:
            raise AgentUnreachable(f"에이전트 {agent_id} 세션 없음") from exc

    def recheck(self, agent_id, start, end) -> bool:
        try:
            return self._agents[agent_id].recheck(start, end)
        except KeyError as exc:
            raise AgentUnreachable(f"에이전트 {agent_id} 없음") from exc

    def commit(self, agent_id, *, event_id, summary, start, end,
              attendee_emails, description="") -> tuple[str, bool]:
        try:
            return self._agents[agent_id].commit(
                event_id=event_id, summary=summary, start=start, end=end,
                attendee_emails=attendee_emails, description=description)
        except KeyError as exc:
            raise AgentUnreachable(f"에이전트 {agent_id} 없음") from exc

    def capabilities(self, agent_id) -> set[str] | None:
        agent = self._agents.get(agent_id)
        return set(getattr(agent, "CAPABILITIES", ())) if agent is not None else None

    def update_event(self, agent_id, *, event_id, start=None, end=None,
                     attendee_emails=None) -> bool:
        try:
            agent = self._agents[agent_id]
        except KeyError as exc:
            raise AgentUnreachable(f"에이전트 {agent_id} 없음") from exc
        return agent.update_event(event_id, start, end, attendee_emails)

    def cancel_event(self, agent_id, event_id) -> bool:
        try:
            agent = self._agents[agent_id]
        except KeyError as exc:
            raise AgentUnreachable(f"에이전트 {agent_id} 없음") from exc
        return agent.cancel_event(event_id)

    def color_settings(self, agent_id) -> dict:
        try:
            return self._agents[agent_id].color_settings_view()
        except KeyError as exc:
            raise AgentUnreachable(f"에이전트 {agent_id} 없음") from exc

    def save_color_settings(self, agent_id, pairs, expected_version=None) -> dict:
        try:
            return self._agents[agent_id].save_color_settings(pairs, expected_version)
        except KeyError as exc:
            raise AgentUnreachable(f"에이전트 {agent_id} 없음") from exc


class HttpTransport:
    """분리된 에이전트 서버를 HTTP 로 호출한다."""

    def __init__(self, endpoints: dict[str, str], auth_token: str,
                 timeout: float = 10.0):
        """endpoints: {agent_id: "http://127.0.0.1:8001"}"""
        self._endpoints = endpoints
        self._token = auth_token
        self._timeout = timeout

    def agent_ids(self) -> list[str]:
        return sorted(self._endpoints)

    def _post(self, agent_id: str, path: str, payload: dict) -> dict:
        import httpx
        url = self._endpoints[agent_id].rstrip("/") + path
        try:
            r = httpx.post(url, json=payload, timeout=self._timeout,
                           headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            raise AgentUnreachable(f"{agent_id} 연결 실패: {exc}") from exc
        if r.status_code == 503:
            raise CalendarUnavailable(f"{agent_id} 캘린더 조회 실패: {r.text}")
        if r.status_code != 200:
            raise AgentHttpError(f"{agent_id} 오류 {r.status_code}: {r.text}",
                                 r.status_code, r.text)
        return r.json()

    def open_negotiation(self, agent_id, session_id, slots, options=None) -> dict:
        payload = {
            "sessionId": session_id,
            "slots": [[s.isoformat(), e.isoformat()] for s, e in slots],
        }
        if options:
            payload["options"] = options
        return self._post(agent_id, "/rpc/negotiation/open", payload)

    def respond(self, agent_id, session_id, round_no, slot_index, balance_hint) -> dict:
        return self._post(agent_id, "/rpc/negotiation/propose", {
            "sessionId": session_id,
            "round": round_no,
            "slotIndex": slot_index,
            "balanceHint": balance_hint,
        })

    def recheck(self, agent_id, start, end) -> bool:
        resp = self._post(agent_id, "/rpc/commit", {
            "recheckOnly": True,
            "start": start.isoformat(),
            "end": end.isoformat(),
        })
        return bool(resp.get("conflict"))

    def commit(self, agent_id, *, event_id, summary, start, end,
              attendee_emails, description="") -> tuple[str, bool]:
        resp = self._post(agent_id, "/rpc/commit", {
            "eventId": event_id,
            "summary": summary,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "attendees": attendee_emails,
            "description": description,
        })
        return resp["eventId"], resp["newlyCreated"]

    def capabilities(self, agent_id) -> set[str] | None:
        """/health 의 capabilities. 응답이 없거나 옛 에이전트(필드 없음)면 빈 집합/None."""
        import httpx
        try:
            r = httpx.get(self._endpoints[agent_id].rstrip("/") + "/health", timeout=3.0)
            if r.status_code != 200:
                return None
            return set(r.json().get("capabilities") or [])
        except (httpx.HTTPError, ValueError, KeyError):
            return None

    def _event_call(self, agent_id, path, payload):
        try:
            return self._post(agent_id, path, payload)
        except AgentHttpError as exc:
            if exc.status == 404 and "event_not_found" in exc.body:
                raise EventNotFound(f"{agent_id}: 이벤트를 찾을 수 없습니다") from exc
            if exc.status in (404, 405):
                raise AgentUnreachable(
                    f"{agent_id} 에이전트가 일정 변경 기능을 지원하지 않습니다 "
                    "(에이전트 서버를 최신 버전으로 다시 시작해야 합니다)") from exc
            raise

    def update_event(self, agent_id, *, event_id, start=None, end=None,
                     attendee_emails=None) -> bool:
        payload: dict = {"eventId": event_id}
        if start is not None and end is not None:
            payload["start"], payload["end"] = start.isoformat(), end.isoformat()
        if attendee_emails is not None:
            payload["attendees"] = attendee_emails
        return bool(self._event_call(agent_id, "/rpc/event/update", payload).get("updated"))

    def cancel_event(self, agent_id, event_id) -> bool:
        return bool(self._event_call(agent_id, "/rpc/event/cancel",
                                     {"eventId": event_id}).get("cancelled"))

    def _get(self, agent_id: str, path: str) -> dict:
        import httpx
        url = self._endpoints[agent_id].rstrip("/") + path
        try:
            r = httpx.get(url, timeout=self._timeout, headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:
            raise AgentUnreachable(f"{agent_id} 연결 실패: {exc}") from exc
        if r.status_code == 503:
            raise CalendarUnavailable(f"{agent_id} 캘린더 조회 실패: {r.text}")
        if r.status_code in (404, 405):
            raise AgentUnreachable(f"{agent_id} 에이전트가 색상 설정 기능을 지원하지 않습니다 "
                                   "(에이전트 서버를 최신 버전으로 다시 시작해야 합니다)")
        if r.status_code != 200:
            raise AgentHttpError(f"{agent_id} 오류 {r.status_code}", r.status_code, r.text)
        return r.json()

    def color_settings(self, agent_id) -> dict:
        return self._get(agent_id, "/rpc/settings/colors")

    def save_color_settings(self, agent_id, pairs, expected_version=None) -> dict:
        try:
            return self._post(agent_id, "/rpc/settings/colors",
                              {"colors": pairs, "expectedVersion": expected_version})
        except AgentHttpError as exc:
            if exc.status == 400:
                try:
                    msg = __import__("json").loads(exc.body).get("error") or "설정을 저장하지 못했습니다"
                except ValueError:
                    msg = "설정을 저장하지 못했습니다"
                raise SettingsError(msg) from None
            if exc.status in (404, 405):
                raise AgentUnreachable(f"{agent_id} 에이전트가 색상 설정 기능을 지원하지 않습니다 "
                                       "(에이전트 서버를 최신 버전으로 다시 시작해야 합니다)") from None
            raise
