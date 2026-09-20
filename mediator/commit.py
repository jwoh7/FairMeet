"""승인 완료 후: 전원 캘린더 재확인 → organizer 1회 커밋 → 잔액 갱신.

이 순서는 바뀌면 안 된다.
  - 재확인을 건너뛰면 승인 대기 중에 생긴 일정 위에 회의를 얹는다
  - 커밋 전에 잔액을 갱신하면 커밋 실패 시 잘못된 부담이 원장에 남는다
  - 참가자별로 insert 를 돌리면 이벤트가 인원수만큼 생긴다
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from agent.calendar_client import CalendarUnavailable, make_event_id
from common.slots import format_slot
from mediator import policy as policy_mod
from mediator import store
from mediator.transport import AgentUnreachable

log = logging.getLogger("fairmeet.commit")

EVENT_SUMMARY = "FairMeet 협의 일정"
EVENT_DESCRIPTION = (
    "FairMeet 에이전트 협상으로 확정된 일정입니다.\n"
    "각 참가자의 개인 사유는 공유되지 않았으며, 참가자 전원이 승인했습니다."
)


def _request_of(row) -> dict:
    """자연어로 만든 협상이면 확인된 회의 요청(제목·목적)을 돌려준다. 없으면 빈 dict."""
    try:
        raw = row["constraints_json"]
    except (KeyError, IndexError):
        return {}
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _summary(row) -> str:
    title = str(_request_of(row).get("title") or "").strip()
    return title[:80] if title and title != "FairMeet 협의 일정" else EVENT_SUMMARY


def _description(policy, total: int, purpose: str = "") -> str:
    """이벤트 설명. 전원 승인이 아닌 회의에서 '전원이 승인했다'는 거짓 문장을 쓰지 않는다.
    누가 승인/거절했는지는 담지 않는다."""
    head = f"목적: {purpose[:200]}\n" if purpose else ""
    if policy.mode == "all":
        return head + EVENT_DESCRIPTION
    return (head + "FairMeet 에이전트 협상으로 확정된 일정입니다.\n"
            "각 참가자의 개인 사유는 공유되지 않았습니다.\n"
            f"승인 기준: 필수 참석자 전원 포함 총 {policy.needed(total)}명 이상 동의.")


class CommitResult:
    def __init__(self, state: str, event_id: str | None = None,
                 newly_created: bool = False, reason: str = "",
                 slot_text: str = ""):
        self.state = state
        self.event_id = event_id
        self.newly_created = newly_created
        self.reason = reason
        self.slot_text = slot_text

    def __repr__(self):
        return (f"<Commit {self.state} event={self.event_id} "
                f"created={self.newly_created} {self.reason}>")


def finalize(session_id: str, transport, emit=None) -> CommitResult:
    """확정을 수행하고, 이 세션이 일정 변경 재협상이었다면 그 결과를 변경 요청에 반영한다."""
    res = _finalize(session_id, transport, emit)
    try:
        from mediator import change_flow
        change_flow.reconcile_for_session(session_id)
    except Exception:                                            # noqa: BLE001
        log.exception("일정 변경 결과 반영 실패")
    return res


def _finalize(session_id: str, transport, emit=None) -> CommitResult:
    """PENDING_HUMAN_APPROVAL(승인 기준 충족) → COMMITTED 까지 수행한다.

    Args:
        transport: InProcessTransport. organizer 의 LocalAgent 를 꺼내 쓴다.
                   중재자가 OAuth 토큰을 갖지 않는 구조를 유지하기 위해
                   커밋은 반드시 organizer 의 에이전트가 수행한다.
    """
    def _emit(ev: dict):
        log.info("%s %s", ev.get("type"), {k: v for k, v in ev.items() if k != "type"})
        if emit:
            emit(ev)

    row = store.get_session(session_id)
    if row is None:
        return CommitResult("UNKNOWN", reason="세션을 찾을 수 없습니다")

    parts = store.get_participants(session_id)
    slots = json.loads(row["slots_json"])
    if row["proposed_slot"] is None:
        return CommitResult(row["state"], reason="제안된 슬롯이 없습니다")

    # 방어선: 승인 기준(기본 = 전원)을 충족하지 못한 제안은 커밋하지 않는다.
    policy = policy_mod.ApprovalPolicy.from_row(row)
    approved_ids, rejected_ids = store.get_verdicts(session_id)
    if row["state"] == "PENDING_HUMAN_APPROVAL" and policy_mod.evaluate(
            policy, [p["agent_id"] for p in parts], approved_ids,
            rejected_ids) != "approved":
        return CommitResult(row["state"], reason="승인 기준을 충족하지 못했습니다")

    start_iso, end_iso = slots[row["proposed_slot"]]
    start = dt.datetime.fromisoformat(start_iso)
    end = dt.datetime.fromisoformat(end_iso)
    slot_text = format_slot(start, end)

    # --- 1) 재확인 상태로 전이 (조건부 → 중복 실행 차단) ---
    if not store.transition(session_id, "PENDING_HUMAN_APPROVAL",
                            "RECHECKING_CALENDARS"):
        cur = store.get_session(session_id)["state"]
        log.info("finalize: 이미 %s 상태이므로 건너뜀", cur)
        return CommitResult(cur, reason="이미 처리된 세션입니다", slot_text=slot_text)

    # 재확인 대상 = 실제로 참석하기로 동의한 사람 + 주최자. 전원 승인 회의에서는 전원이다.
    # 거절했거나 응답하지 않은 선택 참석자의 새 일정이 확정을 막아서는 안 된다.
    recheck_parts = ([p for p in parts
                      if p["agent_id"] in approved_ids or p["is_organizer"]]
                     if policy.mode != "all" else parts) or parts
    _emit({"type": "rechecking", "slot": slot_text, "participants": len(recheck_parts)})

    # --- 2) 동의한 참가자 FreeBusy 재조회 ---
    for p in recheck_parts:
        aid = p["agent_id"]
        try:
            if transport.recheck(aid, start, end):
                store.transition(session_id, "RECHECKING_CALENDARS", "STALE_CONFLICT",
                                 fail_reason=f"{p['display_name']} 일정 충돌")
                _emit({"type": "stale_conflict", "who": p["display_name"]})
                return CommitResult(
                    "STALE_CONFLICT",
                    reason=f"{p['display_name']}님의 캘린더가 승인 대기 중에 변경되었습니다",
                    slot_text=slot_text)
        except AgentUnreachable:
            store.transition(session_id, "RECHECKING_CALENDARS", "AGENT_UNAVAILABLE",
                             fail_reason=f"{aid} 에이전트 없음")
            return CommitResult("AGENT_UNAVAILABLE",
                                reason=f"{p['display_name']}의 에이전트에 연결할 수 없습니다",
                                slot_text=slot_text)
        except CalendarUnavailable as exc:
            store.transition(session_id, "RECHECKING_CALENDARS", "AGENT_UNAVAILABLE",
                             fail_reason=str(exc))
            _emit({"type": "calendar_unavailable", "who": p["display_name"]})
            return CommitResult("AGENT_UNAVAILABLE", reason=str(exc),
                                slot_text=slot_text)

    _emit({"type": "recheck_clear"})

    # --- 3) organizer 캘린더에 단 1회 커밋 ---
    if not store.transition(session_id, "RECHECKING_CALENDARS", "COMMITTING"):
        return CommitResult(store.get_session(session_id)["state"],
                            reason="동시 처리 감지", slot_text=slot_text)

    organizer = next((p for p in parts if p["is_organizer"]), parts[0])
    attendees = [p["email"] for p in parts if p["agent_id"] != organizer["agent_id"]]
    event_id = make_event_id(session_id)

    try:
        if row["change_request_id"]:
            # 일정 변경 재협상: 새 이벤트를 만들지 않고 기존 이벤트의 시간만 고친다.
            # 이 호출이 실패하면 아무것도 바뀌지 않고, 원래 일정이 그대로 남는다.
            parent = store.get_session(row["parent_session_id"])
            eid, created = parent["google_event_id"], False
            transport.update_event(organizer["agent_id"], event_id=eid, start=start, end=end)
        else:
            eid, created = transport.commit(
                organizer["agent_id"], event_id=event_id, summary=_summary(row),
                start=start, end=end, attendee_emails=attendees,
                description=_description(policy, len(parts),
                                         str(_request_of(row).get("purpose") or "")))
    except Exception as exc:
        log.exception("커밋 실패")
        store.transition(session_id, "COMMITTING", "COMMIT_FAILED",
                         fail_reason=str(exc))
        _emit({"type": "commit_failed", "detail": str(exc)})
        return CommitResult("COMMIT_FAILED", reason=str(exc), slot_text=slot_text)

    store.transition(session_id, "COMMITTING", "COMMITTED", google_event_id=eid)

    # --- 4) 커밋 성공 뒤에만 잔액 갱신 (정확히 1회) ---
    applied = store.apply_balance_once(session_id)

    _emit({"type": "committed", "event_id": eid, "newly_created": created,
           "slot": slot_text, "organizer": organizer["display_name"],
           "attendees": len(attendees), "balance_applied": applied})

    return CommitResult("COMMITTED", event_id=eid, newly_created=created,
                        slot_text=slot_text)


def reject(session_id: str, reason: str = "참가자가 거절함") -> bool:
    return store.transition(session_id, "PENDING_HUMAN_APPROVAL", "REJECTED",
                            fail_reason=reason)
