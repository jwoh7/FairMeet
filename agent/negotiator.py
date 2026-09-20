"""에이전트 측 협상 로직. HTTP 서버와 분리되어 있어 단독 테스트가 가능하다.

이 클래스는 한 사용자를 대리한다. 개인 제약과 캘린더 원본은 이 객체 안에
머물고, 밖으로 나가는 것은 숫자 비용 벡터와 accept/counter/reject 뿐이다.
"""
from __future__ import annotations

import datetime as dt

from agent.calendar_client import CalendarBackend, CalendarUnavailable
from agent.calendar_settings import (DEFAULT_COLOR, KIND_LABEL, PALETTE, MemorySettings,
                                     classify_events, color_usage, validate_mapping)
from agent.cost import SCALE_VERSION, cost_vector, feasible_count, parse_buffer_options
from agent.profile import Profile, validate_profile
from common.ids import group_pseudonym, new_nonce
from common.protocol import AgentCard, CostVector, Response

# 이 비용 이하면 즉시 수락. 협상 라운드를 줄이기 위한 임계값.
ACCEPT_THRESHOLD = 35.0
# 역제안을 할 때 '현재 제안보다 이만큼은 나아야' 후보로 올린다.
COUNTER_MARGIN = 15.0
# 잔액이 이보다 낮으면(=과거에 이득을 봤으면) 자발적으로 양보한다.
VOLUNTARY_YIELD_BALANCE = -8.0


class LocalAgent:
    """한 참가자의 개인 에이전트."""

    # 이 에이전트가 처리할 수 있는 선택 기능. 중재자는 /health 로 이를 확인하고, 지원하지 않는
    # 에이전트에게는 해당 조건을 보내지 않은 채 '자동 확인 불가'로 사용자에게 알린다.
    CAPABILITIES = frozenset({"buffer", "event_update", "event_cancel", "color_settings"})

    def __init__(self, profile: Profile, backend: CalendarBackend,
                 group_secret: str, group_id: str, settings_store=None):
        errors = validate_profile(profile)
        if errors:
            raise ValueError(
                f"{profile.display_name} 프로필 검증 실패:\n  - "
                + "\n  - ".join(errors))

        self.profile = profile
        self.backend = backend
        self.agent_id = group_pseudonym(group_secret, group_id, profile.user_key)
        self.settings_store = settings_store if settings_store is not None else MemorySettings()
        self._sessions: dict[str, dict] = {}

    # ---- 발견 ------------------------------------------------------------

    def agent_card(self) -> dict:
        return AgentCard(agent_id=self.agent_id,
                         display_name=f"FairMeet Agent ({self.profile.display_name})"
                         ).to_json()

    # ---- 1단계: 비용 산출 --------------------------------------------------

    def open_negotiation(self, session_id: str,
                         slots: list[tuple[dt.datetime, dt.datetime]],
                         options: dict | None = None) -> dict:
        """캘린더를 조회하고 슬롯별 비용을 계산한다.

        options: 회의 전후 여유 조건 {"before_min", "after_min", "strength"} (선택).

        Raises:
            CalendarUnavailable: 조회 실패. 절대 '한가함'으로 처리하지 않는다.
            ValueError: 슬롯이 비었거나 옵션이 올바르지 않음.
        """
        if not slots:
            raise ValueError("슬롯이 비어 있습니다")
        buffer = parse_buffer_options(options)

        # 여유 구간의 일정도 봐야 하므로 조회 범위를 그만큼 넓힌다.
        pad_before = dt.timedelta(minutes=buffer["before_min"]) if buffer else dt.timedelta()
        pad_after = dt.timedelta(minutes=buffer["after_min"]) if buffer else dt.timedelta()
        events = self.backend.fetch_events(slots[0][0] - pad_before, slots[-1][1] + pad_after)
        classified = classify_events(events, self.settings_store.load())

        costs, veto = cost_vector(slots, classified, buffer)
        self._sessions[session_id] = {"slots": slots, "costs": costs, "veto": veto}

        return CostVector(
            session_id=session_id,
            agent_id=self.agent_id,
            nonce=new_nonce(),
            issued_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            costs=costs,
            veto_mask=veto,
            scale_version=SCALE_VERSION,
        ).to_json()

    # ---- 2단계: 제안에 응답 -------------------------------------------------

    def respond(self, session_id: str, round_no: int, slot_index: int,
                balance_hint: float = 0.0) -> dict:
        """제안된 슬롯을 수락/역제안/거절한다.

        balance_hint 는 중재자가 알려주는 자기 잔액이다. 과거에 이득을 본
        상태(음수)면 자발적으로 양보한다 — 이것이 협상을 빨리 수렴시킨다.
        """
        st = self._sessions.get(session_id)
        if st is None:
            raise KeyError(f"알 수 없는 세션: {session_id}")

        costs = st["costs"]
        if not (0 <= slot_index < len(costs)):
            raise IndexError(f"슬롯 인덱스 범위 밖: {slot_index}")

        c = costs[slot_index]

        # 거부권 — 사유는 전달하지 않는다
        if c is None:
            alts = self._best_alternatives(costs, None)
            return Response(session_id, self.agent_id, round_no,
                            "reject", alts).to_json()

        # 비용이 낮거나, 과거에 이득을 봤으면 수락
        if c <= ACCEPT_THRESHOLD or balance_hint < VOLUNTARY_YIELD_BALANCE:
            return Response(session_id, self.agent_id, round_no,
                            "accept", []).to_json()

        alts = self._best_alternatives(costs, c - COUNTER_MARGIN)
        if not alts:
            return Response(session_id, self.agent_id, round_no,
                            "accept", []).to_json()
        return Response(session_id, self.agent_id, round_no,
                        "counter", alts).to_json()

    @staticmethod
    def _best_alternatives(costs, ceiling: float | None, k: int = 3) -> list[int]:
        """자기 기준 더 나은 슬롯 최대 k개.

        k 를 크게 잡으면 비용 벡터 전체를 역추론당할 수 있으므로 제한한다.
        """
        cands = [(c, i) for i, c in enumerate(costs)
                 if c is not None and (ceiling is None or c < ceiling)]
        cands.sort()
        return [i for _, i in cands[:k]]

    # ---- 3단계: 커밋 (organizer 만) ------------------------------------------

    def recheck(self, start: dt.datetime, end: dt.datetime) -> bool:
        """커밋 직전 재확인. True 면 충돌이 있다 (고정 일정과 겹칠 때만; 유동 일정은 이미 감수한 비용이다)."""
        classified = classify_events(self.backend.fetch_events(start, end), self.settings_store.load())
        return any(k == "fixed" and s < end and start < e for s, e, k in classified)

    def commit(self, event_id: str, summary: str,
               start: dt.datetime, end: dt.datetime,
               attendee_emails: list[str], description: str = "") -> tuple[str, bool]:
        """organizer 의 에이전트만 호출한다. 중재자는 토큰을 갖지 않는다."""
        return self.backend.commit_event(
            event_id=event_id, summary=summary, start=start, end=end,
            attendee_emails=attendee_emails, description=description)

    def update_event(self, event_id: str, start: dt.datetime | None = None,
                     end: dt.datetime | None = None,
                     attendee_emails: list[str] | None = None) -> bool:
        """이미 만든 이벤트를 고친다(일정 변경). organizer 의 에이전트만 호출한다."""
        return self.backend.update_event(event_id=event_id, start=start, end=end,
                                         attendee_emails=attendee_emails)

    def cancel_event(self, event_id: str) -> bool:
        """이벤트를 취소한다. 이미 없으면 False."""
        return self.backend.cancel_event(event_id)

    def feasible_slots(self, session_id: str) -> int:
        st = self._sessions.get(session_id)
        return feasible_count(st["costs"]) if st else 0

    # ---- 일정 종류 설정 (색상 → 고정 / 유동 / 무시) --------------------------------------------------

    def color_settings_view(self, today: dt.datetime | None = None, days: int = 45) -> dict:
        """설정 화면용. 색상 팔레트, 사용자가 정한 종류, 색상별 일정 개수. 일정의 제목·시각은 담지 않는다."""
        settings = self.settings_store.load()
        now = today or dt.datetime.now(dt.timezone.utc)
        usage = color_usage(self.backend.fetch_events(now, now + dt.timedelta(days=days)))
        colors = [{"colorId": cid, "name": name, "hex": hexv,
                   "kind": settings.colors.get(cid, "fixed"),
                   "chosen": cid in settings.colors, "count": usage.get(cid, 0)}
                  for cid, (name, hexv) in PALETTE.items()]
        return {"colors": colors, "version": settings.version, "configured": settings.configured,
                "uncolored": {"colorId": DEFAULT_COLOR, "count": usage.get(DEFAULT_COLOR, 0),
                              "kind": "fixed", "label": "색상을 지정하지 않은 일정"},
                "kinds": KIND_LABEL,
                "note": ("일정 색상 분류를 설정하지 않아 모든 기존 일정을 고정으로 처리합니다"
                         if not settings.configured else
                         "색상을 지정하지 않은 일정, 알 수 없는 색상, 조회하지 못한 일정은 고정으로 처리합니다")}

    def save_color_settings(self, pairs, expected_version: int | None = None) -> dict:
        """[{colorId, kind}] 를 검증해 저장한다. Raises: SettingsError."""
        mapping = validate_mapping(pairs)
        saved = self.settings_store.save(mapping, expected_version)
        return {"version": saved.version, "configured": saved.configured}