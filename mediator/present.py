"""사용자 화면용 뷰: 내부 상태·오류를 사용자 문장으로 바꾸고, 내부 정보는 담지 않는다.

기본(사용자 모드) 뷰에 없는 것: 양보 잔액, 개인별 regret, 비용 벡터, 에이전트 ID, 원시 협상 로그, 토큰·승인 링크,
DB 정보, 내부 상태 전이 코드, 서버 오류 원문, 개인 일정 제목과 사유, 이메일 주소, 거절한 사람.
시연 모드 뷰(demo_view)도 개인 regret·일정 제목·거절 사유·이메일은 담지 않는다.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from common import config
from common.slots import format_slot
from mediator import approval_flow, change_flow, store

# ---- 상태 -------------------------------------------------------------------------------------

STATE_GROUP = {
    "OPEN": "progress", "COSTS_COLLECTED": "progress", "AGENT_NEGOTIATING": "progress",
    "RECHECKING_CALENDARS": "progress", "COMMITTING": "progress", "STALE_CONFLICT": "progress",
    "PENDING_HUMAN_APPROVAL": "waiting", "COMMITTED": "confirmed",
    "EXPIRED": "failed", "NO_FEASIBLE_SLOT": "failed", "AGENT_UNAVAILABLE": "failed",
    "COMMIT_FAILED": "failed", "NO_COMMON_SLOT": "failed", "CANDIDATES_EXHAUSTED": "failed",
    "MAX_APPROVAL_ROUNDS_REACHED": "failed", "REJECTED": "failed",
    "CANCELLED": "cancelled", "RESCHEDULED": "cancelled",
}
GROUP_LABEL = {"progress": "진행 중", "waiting": "동의 대기", "confirmed": "확정",
               "failed": "실패", "cancelled": "취소"}

HEADLINE = {
    "OPEN": ("회의 요청을 준비하고 있어요", "잠시만 기다려 주세요."),
    "COSTS_COLLECTED": ("공정하게 조정 중이에요", "참가자들이 가능한 시간을 서로 맞춰 보고 있어요."),
    "AGENT_NEGOTIATING": ("공정하게 조정 중이에요", "참가자들이 가능한 시간을 서로 맞춰 보고 있어요."),
    "PENDING_HUMAN_APPROVAL": ("참가자의 동의를 기다리고 있어요",
                               "제안한 시간을 이메일로 보냈어요. 응답이 모이면 자동으로 확정돼요."),
    "RECHECKING_CALENDARS": ("확정하기 전에 일정을 다시 확인하고 있어요", "곧 캘린더에 등록돼요."),
    "COMMITTING": ("캘린더에 등록하고 있어요", "곧 확정돼요."),
    "STALE_CONFLICT": ("다음 가능한 시간을 확인하고 있어요",
                       "그 사이 참가자의 일정이 바뀌어 다른 시간을 찾고 있어요."),
    "COMMITTED": ("회의가 확정됐어요", "참가자 캘린더에 등록됐어요."),
    "EXPIRED": ("응답 기한이 지나 요청이 끝났어요", "새 회의 요청으로 다시 시도해 보세요."),
    "AGENT_UNAVAILABLE": ("참가자의 일정을 확인하지 못했어요",
                          "잠시 후 다시 시도해 주세요. 확인하지 못한 채로 일정을 만들지는 않아요."),
    "COMMIT_FAILED": ("캘린더에 등록하지 못했어요", "일정이 만들어지지 않았어요. 잠시 후 다시 시도해 주세요."),
    "NO_COMMON_SLOT": ("모두가 가능한 시간을 찾지 못했어요", "날짜 범위를 넓혀보세요."),
    "NO_FEASIBLE_SLOT": ("모두가 가능한 시간을 찾지 못했어요", "날짜 범위를 넓혀보세요."),
    "CANDIDATES_EXHAUSTED": ("제안할 수 있는 시간을 모두 확인했어요", "날짜 범위를 넓히거나 회의 시간을 줄여보세요."),
    "MAX_APPROVAL_ROUNDS_REACHED": ("정해 둔 횟수만큼 제안했어요", "날짜 범위를 넓히거나 회의 시간을 줄여보세요."),
    "REJECTED": ("제안이 받아들여지지 않았어요", "새 회의 요청으로 다시 시도해 보세요."),
    "CANCELLED": ("회의가 취소됐어요", "참가자 캘린더에서도 삭제됐어요."),
    "RESCHEDULED": ("새 시간으로 옮겨졌어요", ""),
}
REPROPOSED = ("제안한 시간이 어려워 다음 가능한 시간을 확인하고 있어요",
              "새로 제안한 시간으로 다시 동의를 받고 있어요.")

ACTION_LABEL = {"widen_range": "날짜 범위를 넓혀 다시 찾기", "shorten_duration": "회의 시간을 줄여 다시 찾기",
                "close": "이번 요청 닫기"}

_STEPS = ("요청 접수", "시간 조정", "참가자 동의", "캘린더 등록")


def status_group(state: str) -> str:
    return STATE_GROUP.get(state, "progress")


# ---- 개인정보·비밀값 필터 (출력 단계) --------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b")
_LONG = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")
_PATH = re.compile(r"(?:[A-Za-z]:\\[^\s\"']+|/(?:Users|home|etc|var|usr|root)/[^\s\"']+)")
_TRACE = re.compile(r"Traceback \(most recent call last\)|File \"[^\"]+\", line \d+")


def scrub_text(text: str) -> str:
    t = _TRACE.sub("(내부 오류 정보 숨김)", str(text))
    t = _EMAIL.sub("(이메일 비공개)", t)
    t = _TOKEN.sub("(비공개)", t)
    t = _PATH.sub("(경로 숨김)", t)
    return _LONG.sub("(비공개)", t)


def scrub(obj):
    """응답에 들어가는 모든 문자열에서 이메일·토큰·서버 경로·스택트레이스를 지운다."""
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    return obj


# ---- 회의 목록 · 상세 ---------------------------------------------------------------------------

def _title_of(s) -> str:
    raw = s["constraints_json"]
    if raw:
        try:
            d = json.loads(raw)
            title = (d.get("title") or "").strip()
            if title and title != "FairMeet 협의 일정":          # 기본 제목이면 쓸모가 없으니 내용으로 대신한다
                return title[:60]
            if d.get("purpose"):
                return str(d["purpose"]).strip()[:60]
            return f"{d.get('duration_min') or s['duration_min']}분 회의"
        except ValueError:
            pass
    if s["change_request_id"]:
        return "일정 변경 재협상"
    return "팀 회의"


def _followup_of(c, session_id: str):
    return c.execute(
        "SELECT s.* FROM change_requests r JOIN sessions s ON s.session_id = r.followup_session_id "
        "WHERE r.session_id = ? AND r.state = 'applied' AND r.kind = 'renegotiate' "
        "ORDER BY r.updated_at DESC LIMIT 1", (session_id,)).fetchone()


def _slot_text_of(s) -> str | None:
    if s["proposed_slot"] is None:
        return None
    a, b = json.loads(s["slots_json"])[s["proposed_slot"]]
    return format_slot(dt.datetime.fromisoformat(a), dt.datetime.fromisoformat(b))


def _summary(s, effective) -> dict:
    state = effective["state"]
    group = status_group(state)
    head = HEADLINE.get(state, ("진행 중이에요", ""))
    if state == "PENDING_HUMAN_APPROVAL" and effective["proposal_version"] > 1:
        head = REPROPOSED
    return {"id": s["session_id"], "title": _title_of(s), "group": group,
            "groupLabel": GROUP_LABEL[group], "headline": head[0],
            "slot": _slot_text_of(effective) if group in ("waiting", "confirmed") or (
                group == "progress" and effective["proposed_slot"] is not None) else None,
            "createdAt": s["created_at"], "updatedAt": effective["updated_at"]}


def list_meetings(group: str | None = None, limit: int = 40) -> list[dict]:
    """원래 회의 목록. 일정 변경으로 생긴 재협상 세션은 원래 회의 안에서 보여 주므로 따로 세지 않는다."""
    change_flow.expire_stale_changes()
    with store.connect() as c:
        rows = c.execute("SELECT * FROM sessions WHERE change_request_id IS NULL "
                         "ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 100)),)).fetchall()
        out = []
        for s in rows:
            eff = s
            if s["state"] == "RESCHEDULED":
                eff = _followup_of(c, s["session_id"]) or s
            out.append(_summary(s, eff))
    if group and group != "all":
        out = [m for m in out if m["group"] == group]
    return out


def _steps(state: str, pending: bool) -> list[dict]:
    idx = {"OPEN": 1, "COSTS_COLLECTED": 1, "AGENT_NEGOTIATING": 1, "PENDING_HUMAN_APPROVAL": 2,
           "STALE_CONFLICT": 1, "RECHECKING_CALENDARS": 3, "COMMITTING": 3}.get(state)
    failed = status_group(state) == "failed"
    done_all = state in ("COMMITTED", "CANCELLED", "RESCHEDULED")
    out = []
    for i, label in enumerate(_STEPS):
        if done_all or (idx is not None and i < idx) or i == 0:
            st = "done"
        elif idx is not None and i == idx:
            st = "current"
        else:
            st = "todo"
        out.append({"label": label, "state": st})
    if failed:
        # 어느 단계에서 멈췄는지는 종료 사유로 가늠한다 (동의 단계 이후 실패는 3, 그 전은 2)
        stop = 3 if state in ("EXPIRED", "CANDIDATES_EXHAUSTED", "MAX_APPROVAL_ROUNDS_REACHED",
                              "REJECTED") else 2
        if state == "COMMIT_FAILED":
            stop = 4
        for i, step in enumerate(out):
            step["state"] = "done" if i < stop - 1 else ("failed" if i == stop - 1 else "todo")
    return out


def _understood(s, v: dict) -> dict:
    """시스템이 이해한 요청 조건 요약 (사용자가 협상 시작 뒤에도 확인할 수 있다)."""
    slots = json.loads(s["slots_json"])
    span = None
    if slots:
        a = dt.datetime.fromisoformat(slots[0][0]).date()
        b = dt.datetime.fromisoformat(slots[-1][0]).date()
        span = f"{a:%m월 %d일} ~ {b:%m월 %d일}"
    cond = v.get("conditions") or {}
    items = []
    for i in cond.get("items", []):
        items.append({"strength": "필수" if i["strength"] == "hard" else "선호",
                      "text": i.get("explanation") or i.get("sourceText") or "",
                      "manual": (not i.get("supported", True)) and True,
                      "manualAck": bool(i.get("acknowledged"))})
    return {"durationMin": s["duration_min"], "period": span,
            "approval": v["policy"]["description"], "required": v["policy"]["required"],
            "conditions": items, "manualChecks": [re.sub(r"^'[^']*':\s*", "", m)
                                                  for m in cond.get("manualChecks", [])]}


def meeting_detail(session_id: str) -> dict | None:
    s = store.get_session(session_id)
    if s is None:
        return None
    approval_flow.settle_expired_sessions()      # 기한 경과 세션은 정족수를 다시 검증해 결정한다
    change_flow.expire_stale_changes()
    change_flow.reconcile_all()
    s = store.get_session(session_id)
    with store.connect() as c:
        eff = _followup_of(c, session_id) if s["state"] == "RESCHEDULED" else None
    eff = eff or s
    v = approval_flow.session_view(eff["session_id"])
    state = eff["state"]
    pending = state == approval_flow.PENDING
    group = status_group(state)
    head = HEADLINE.get(state, ("진행 중이에요", ""))
    reproposed = pending and eff["proposal_version"] > 1
    if reproposed:
        head = REPROPOSED
    slot = _slot_text_of(eff) if (pending or group == "confirmed") else None

    people, responded = [], 0
    for p in v["participants"]:
        if not pending:
            people.append({"name": p["name"], "state": "none", "label": ""})
            continue
        st = p["status"]
        if st == "responded":
            responded += 1
            people.append({"name": p["name"], "state": "responded", "label": "응답 완료"})
        elif st == "send_failed":
            people.append({"name": p["name"], "state": "unsent", "label": "메일 전달 확인 필요"})
        else:
            people.append({"name": p["name"], "state": "waiting", "label": "응답 대기"})

    alert = None
    n = v["notification"]
    if pending and n["status"] in ("partial_failure", "all_failed", "pending") and n["retryable"]:
        alert = {"text": "일부 참가자에게 동의 요청 메일이 아직 전달되지 않았어요.", "action": "retry_mail",
                 "actionLabel": "다시 보내기"}
    actions = []
    if group == "failed" and v.get("outcome"):
        actions = [{"action": o["action"], "label": ACTION_LABEL.get(o["action"], o["label"])}
                   for o in v["outcome"]["options"]]

    change = change_flow.change_view(session_id)
    change_out = None
    if change:
        reqs = [{"kind": r["kindLabel"], "state": r["state"], "stateLabel": r["stateLabel"],
                 "voted": r["votes"]["responded"], "voters": r["votes"]["total"],
                 "open": r["state"] in change_flow.OPEN_STATES}
                for r in change["requests"]]
        mails = change["confirmationMails"] or {}
        # 참가자가 보낸 '확인이 필요한 변경 요청'(접수). 처리 방법은 주최자가 이 카드에서 고른다.
        intakes = [{"intakeId": i["intakeId"], "requester": i["requester"], "slot": i["slot"],
                    "createdAt": i["createdAt"], "state": i["state"],
                    "decisionLabel": i["decisionLabel"],
                    "options": [{"action": o["action"], "label": o["label"], "hint": o["hint"],
                                 "enabled": o["enabled"], "reason": o["reason"]}
                                for o in i["options"]]}
                   for i in change_flow.intake_cards(session_id, only_open=False)]
        open_intakes = [i for i in intakes if i["state"] == "open"]
        change_out = {"requests": reqs,
                      "intakes": intakes,
                      "openIntakes": len(open_intakes),
                      "open": any(r["open"] for r in reqs) or bool(open_intakes),
                      "linksSent": (mails.get("sent") or 0),
                      "linksMissing": (mails.get("failed") or 0) + (mails.get("pending") or 0)}

    closed = False
    if group == "failed":
        with store.connect() as c:
            closed = c.execute("SELECT 1 FROM transitions WHERE session_id = ? "
                               "AND detail = 'closed by organizer'", (eff["session_id"],)
                               ).fetchone() is not None

    return scrub({
        "id": session_id, "title": _title_of(s), "group": group, "groupLabel": GROUP_LABEL[group],
        "headline": head[0], "detail": head[1], "reproposed": reproposed,
        "slot": slot, "active": group in ("progress", "waiting") or bool(change_out and change_out["open"]),
        "steps": _steps(state, pending),
        "progress": {"responded": responded, "total": len(people), "needed": v["policy"]["needed"],
                     "people": people, "show": pending},
        "understood": _understood(s, v),
        "alert": alert, "actions": [] if closed else actions, "closed": closed,
        "change": change_out,
        "isFollowup": bool(s["change_request_id"]),
        "movedFrom": None,
        "createdAt": s["created_at"], "updatedAt": eff["updated_at"],
        "confirmed": group == "confirmed",
        "effectiveId": eff["session_id"],
    })


# ---- 시연 모드 뷰 ------------------------------------------------------------------------------

_PHASE = {
    "OPEN": "회의 요청 접수", "COSTS_COLLECTED": "참가자 에이전트가 비용만 전달 (제목·사유는 교환하지 않음)",
    "AGENT_NEGOTIATING": "양보 잔액을 반영해 공정한 후보 선택", "PENDING_HUMAN_APPROVAL": "참가자 동의 대기",
    "RECHECKING_CALENDARS": "확정 전 캘린더 재확인", "COMMITTING": "캘린더 등록", "COMMITTED": "확정",
    "STALE_CONFLICT": "일정 충돌 감지 → 다음 후보", "EXPIRED": "응답 시간 초과",
    "NO_FEASIBLE_SLOT": "공통 시간 없음", "NO_COMMON_SLOT": "공통 시간 없음",
    "CANDIDATES_EXHAUSTED": "후보 소진", "MAX_APPROVAL_ROUNDS_REACHED": "최대 제안 횟수 도달",
    "AGENT_UNAVAILABLE": "일정 확인 실패 (안전하게 중단)", "COMMIT_FAILED": "등록 실패", "REJECTED": "종료",
    "CANCELLED": "취소", "RESCHEDULED": "새 시간으로 이동",
}
_PROPOSAL_LABEL = {"open": "동의 대기", "approved": "전원 동의", "rejected": "받아들여지지 않음",
                   "conflict": "일정 충돌", "expired": "시간 초과"}


def demo_ledger(group_id: str, agent_names: dict[str, str]) -> dict:
    bal = store.get_balance(group_id, list(agent_names))
    entries = [{"name": agent_names[a], "balance": round(v, 2)}
               for a, v in sorted(bal.items(), key=lambda kv: -kv[1])]
    values = [e["balance"] for e in entries] or [0.0]
    spread = round(max(values) - min(values), 2)
    return {"entries": entries, "spread": spread,
            "total": round(sum(values), 2),
            "verdict": "균형" if spread < 5 else ("약간 한쪽으로 치우침" if spread < 15 else "한쪽으로 치우침"),
            "note": "잔액은 과거 양보의 누적입니다. 개인별 불편 점수, 일정 제목, 사유는 표시하지 않습니다."}


def demo_view(session_id: str, agent_names: dict[str, str]) -> dict | None:
    s = store.get_session(session_id)
    if s is None:
        return None
    with store.connect() as c:
        trans = c.execute("SELECT to_state, at FROM transitions WHERE session_id = ? ORDER BY id",
                          (session_id,)).fetchall()
    phases, last = [], None
    for t in trans:
        if t["to_state"] == last:
            continue
        last = t["to_state"]
        phases.append({"at": t["at"], "text": _PHASE.get(t["to_state"], "진행")})
    props = store.get_proposals(session_id)
    return scrub({
        "ledger": demo_ledger(s["group_id"], agent_names),
        "phases": phases,
        "proposals": [{"rank": p["version"], "status": p["status"],
                       "label": _PROPOSAL_LABEL.get(p["status"], p["status"]),
                       "slot": _slot_text_of({"slots_json": s["slots_json"], "proposed_slot": p["slot_index"]})}
                      for p in props],
        "participants": len(store.get_participants(session_id)),
        "exchange": "각 에이전트는 시간대별 0~100 비용과 거부권 플래그만 전달했어요.",
    })
