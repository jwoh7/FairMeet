"""협상 라운드 루프.

라운드 진행:
  Round 0  전원 비용 벡터 수집 → regret 행렬 계산 (이후 고정)
  Round r  preferred_set ∩ candidate_mask 에서 solve
           → 전원에게 제안 → 전원 accept 면 승인 단계로
           → 아니면 그 슬롯을 mask 에서 빼고, 역제안을 preferred_set 에 누적

역제안(counterIndices)이 실제로 다음 라운드의 탐색 공간을 좁힌다.
역제안을 무시하고 슬롯만 제외하는 것은 협상이라고 부를 수 없다.

이 모듈은 '에이전트 간' 협상만 다룬다. 합의 후 사람이 거절했을 때 다음 후보로
다시 제안하는 '사람 승인 라운드'는 mediator/approval_flow.py 가 담당한다.
공통 가능 슬롯이 0개면 여기서 즉시 NO_COMMON_SLOT 으로 끝난다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from agent.calendar_client import CalendarUnavailable
from common import config
from common.fairness import NoFeasibleAgent, build_regret_matrix, solve
from common.slots import format_slot
from mediator import store
from mediator.payload_guard import PayloadViolation, validate_cost_payload, \
    validate_response_payload
from mediator.transport import AgentUnreachable, Transport

log = logging.getLogger("fairmeet.negotiation")

SCALE_VERSION = "cost-rules-v1"


class NegotiationOutcome:
    """협상 결과. state 가 결과의 진실이다."""

    def __init__(self, session_id: str, state: str, slot_index: int | None = None,
                 regrets: dict | None = None, reason: str = "",
                 rounds: int = 0, events: list | None = None):
        self.session_id = session_id
        self.state = state
        self.slot_index = slot_index
        self.regrets = regrets or {}
        self.reason = reason
        self.rounds = rounds
        self.events = events or []

    def __repr__(self):
        return (f"<Negotiation {self.state} slot={self.slot_index} "
                f"rounds={self.rounds} {self.reason}>")


def run_negotiation(
    session_id: str,
    transport: Transport,
    slots: list[tuple[dt.datetime, dt.datetime]],
    group_id: str,
    lam: float | None = None,
    max_rounds: int | None = None,
    emit=None,
) -> NegotiationOutcome:
    """세션 하나의 협상을 끝까지(또는 실패까지) 진행한다.

    Args:
        emit: 대시보드용 콜백 emit(dict). None 이면 로그만 남긴다.

    Returns:
        NegotiationOutcome. state 는 PENDING_HUMAN_APPROVAL 또는 실패 상태.
    """
    max_rounds = max_rounds if max_rounds is not None else config.MAX_ROUNDS
    lam = lam if lam is not None else config.LAMBDA_WORK
    events: list[dict] = []

    def _emit(ev: dict):
        events.append(ev)
        log.info("%s %s", ev.get("type"), {k: v for k, v in ev.items() if k != "type"})
        if emit:
            emit(ev)

    n_slots = len(slots)
    # 협상에는 이 세션의 참가자(사용자가 확정해 스냅샷으로 저장한 사람)만 들어간다. 에이전트가 더 많이 연결돼 있어도
    # 참가자가 아닌 사람의 캘린더는 조회하지 않는다. 참가자 기록이 없는 세션(옛 테스트)만 연결된 전체를 쓴다.
    session_parts = sorted(p["agent_id"] for p in store.get_participants(session_id))
    agent_ids = session_parts or transport.agent_ids()

    # 회의 조건(hard 제외 슬롯, soft 페널티, 에이전트 옵션)은 세션을 만들 때 한 번 저장된 값을
    # 그대로 쓴다. 조건이 없는 세션(수동 협상, 옛 세션)은 (빈 집합, 빈 dict, 옵션 없음).
    sess = store.get_session(session_id)
    excluded, penalty = store.slot_effects_of(sess) if sess is not None else (set(), {})
    agent_options = store.agent_options_of(sess) if sess is not None else {}

    # ---- Round 0: 비용 수집 -------------------------------------------------
    costs: dict[str, list[float | None]] = {}
    for aid in agent_ids:
        try:
            if agent_options.get(aid):
                payload = transport.open_negotiation(aid, session_id, slots,
                                                     agent_options[aid])
            else:
                payload = transport.open_negotiation(aid, session_id, slots)
        except CalendarUnavailable as exc:
            # fail-closed: 조회 실패를 '한가함'으로 처리하지 않는다
            store.transition(session_id, "OPEN", "AGENT_UNAVAILABLE",
                             fail_reason=f"calendar: {exc}")
            _emit({"type": "calendar_unavailable", "agent": aid, "detail": str(exc)})
            return NegotiationOutcome(session_id, "AGENT_UNAVAILABLE",
                                      reason=str(exc), events=events)
        except AgentUnreachable as exc:
            store.transition(session_id, "OPEN", "AGENT_UNAVAILABLE",
                             fail_reason=str(exc))
            _emit({"type": "agent_unreachable", "agent": aid, "detail": str(exc)})
            return NegotiationOutcome(session_id, "AGENT_UNAVAILABLE",
                                      reason=str(exc), events=events)

        try:
            validate_cost_payload(payload, n_slots)
        except PayloadViolation as exc:
            store.transition(session_id, "OPEN", "AGENT_UNAVAILABLE",
                             fail_reason=f"payload: {exc}")
            _emit({"type": "payload_rejected", "agent": aid, "detail": str(exc)})
            return NegotiationOutcome(session_id, "AGENT_UNAVAILABLE",
                                      reason=f"payload 위반: {exc}", events=events)

        if payload["scaleVersion"] != SCALE_VERSION:
            reason = (f"비용 규칙 버전 불일치: {aid} 가 "
                      f"{payload['scaleVersion']}, 기대값 {SCALE_VERSION}")
            store.transition(session_id, "OPEN", "AGENT_UNAVAILABLE",
                             fail_reason=reason)
            _emit({"type": "scale_mismatch", "agent": aid})
            return NegotiationOutcome(session_id, "AGENT_UNAVAILABLE",
                                      reason=reason, events=events)

        costs[payload["agentId"]] = payload["costs"]

    _emit({"type": "costs_collected", "agents": len(costs),
           "slots": n_slots, "scale": SCALE_VERSION,
           "free_text_fields": 0})

    # ---- regret 행렬: 단 한 번만 계산 -----------------------------------------
    # 공통 가능 슬롯이 0개면 (한 명이 모든 슬롯에 거부권을 걸었든, 각자 가능한 시간이
    # 겹치지 않든) 사람에게 아무것도 묻지 않고 즉시 NO_COMMON_SLOT 으로 끝낸다.
    try:
        regret, baselines = build_regret_matrix(costs)
    except NoFeasibleAgent as exc:
        store.transition(session_id, "OPEN", "NO_COMMON_SLOT", fail_reason=str(exc))
        _emit({"type": "no_common_slot", "detail": str(exc)})
        return NegotiationOutcome(session_id, "NO_COMMON_SLOT",
                                  reason=str(exc), events=events)

    common = {i for i in range(n_slots)
              if all(regret[a][i] is not None for a in regret)}
    candidate_mask = common - excluded          # hard 회의 조건을 어기는 슬롯은 후보가 아니다
    if not candidate_mask:
        why = ("회의 조건을 만족하면서 전원이 가능한 시간이 없습니다"
               if common else "전원이 동시에 가능한 슬롯이 없습니다")
        store.transition(session_id, "OPEN", "NO_COMMON_SLOT", fail_reason=why)
        _emit({"type": "no_common_slot", "detail": why if common else "공통 가능 슬롯 0개"})
        return NegotiationOutcome(session_id, "NO_COMMON_SLOT", reason=why, events=events)

    store.save_regret_matrix(session_id, regret, candidate_mask, lam=lam)
    store.transition(session_id, "COSTS_COLLECTED", "AGENT_NEGOTIATING")
    _emit({"type": "feasible", "common_slots": len(candidate_mask)})

    balance = store.get_balance(group_id, list(regret))
    if any(abs(v) > 1e-9 for v in balance.values()):
        _emit({"type": "balance_loaded",
               "balance": {k: round(v, 1) for k, v in balance.items()}})

    preferred_set: set[int] = set()

    # ---- 협상 라운드 ---------------------------------------------------------
    for rnd in range(1, max_rounds + 1):
        search = (preferred_set & candidate_mask) or candidate_mask
        result = solve(regret, balance, search, lam=lam, slot_penalty=penalty)
        if result is None and search is not candidate_mask:
            result = solve(regret, balance, candidate_mask, lam=lam, slot_penalty=penalty)

        if result is None:
            store.transition(session_id, "AGENT_NEGOTIATING", "CANDIDATES_EXHAUSTED",
                             fail_reason="후보 슬롯 소진")
            _emit({"type": "candidates_exhausted", "round": rnd})
            return NegotiationOutcome(session_id, "CANDIDATES_EXHAUSTED",
                                      reason="후보 슬롯이 모두 소진되었습니다",
                                      rounds=rnd, events=events)

        s, e = slots[result.slot_index]
        _emit({"type": "propose", "round": rnd, "slot_index": result.slot_index,
               "slot": format_slot(s, e),
               "max_adjusted": round(result.max_adjusted, 1),
               "regrets": {k: round(v, 1) for k, v in result.regrets.items()},
               "searched": len(search)})

        verdicts, counters = [], set()
        unreachable = None
        for aid in agent_ids:
            try:
                resp = transport.respond(aid, session_id, rnd,
                                         result.slot_index, balance.get(aid, 0.0))
                validate_response_payload(resp, n_slots)
            except (AgentUnreachable, PayloadViolation) as exc:
                unreachable = (aid, str(exc))
                break
            verdicts.append((resp["agentId"], resp["verdict"]))
            counters.update(resp.get("counterIndices", []))

        if unreachable:
            aid, detail = unreachable
            store.transition(session_id, "AGENT_NEGOTIATING", "AGENT_UNAVAILABLE",
                             fail_reason=detail)
            _emit({"type": "agent_unreachable", "agent": aid, "detail": detail})
            return NegotiationOutcome(session_id, "AGENT_UNAVAILABLE",
                                      reason=detail, rounds=rnd, events=events)

        _emit({"type": "responses", "round": rnd,
               "verdicts": verdicts,
               "counter_indices": sorted(counters)})

        if all(v == "accept" for _, v in verdicts):
            store.open_approval_window(session_id, result.slot_index, result.regrets)
            _emit({"type": "agreed", "round": rnd,
                   "slot": format_slot(s, e),
                   "worst_off": max(result.regrets, key=result.regrets.get)})
            return NegotiationOutcome(session_id, "PENDING_HUMAN_APPROVAL",
                                      slot_index=result.slot_index,
                                      regrets=result.regrets, rounds=rnd,
                                      events=events)

        # 합의 실패 → 이 슬롯 제외, 역제안을 다음 탐색 공간으로
        candidate_mask.discard(result.slot_index)
        preferred_set = counters & candidate_mask
        store.advance_round(session_id, candidate_mask, preferred_set, rnd)

        if not candidate_mask:
            store.transition(session_id, "AGENT_NEGOTIATING", "CANDIDATES_EXHAUSTED",
                             fail_reason="후보 슬롯 소진")
            _emit({"type": "candidates_exhausted", "round": rnd})
            return NegotiationOutcome(session_id, "CANDIDATES_EXHAUSTED",
                                      reason="후보 슬롯이 모두 소진되었습니다",
                                      rounds=rnd, events=events)

    # 라운드 소진 — 사람에게 넘긴다
    store.transition(session_id, "AGENT_NEGOTIATING", "NO_FEASIBLE_SLOT",
                     fail_reason=f"{max_rounds}라운드 내 미합의")
    _emit({"type": "max_rounds_exceeded", "rounds": max_rounds})
    return NegotiationOutcome(session_id, "NO_FEASIBLE_SLOT",
                              reason=f"{max_rounds}라운드 내에 합의하지 못했습니다",
                              rounds=max_rounds, events=events)
