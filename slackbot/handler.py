"""Slack 승인 (선택 사항 — 웹 승인이 기본 경로다).

48시간 MVP에서는 빼는 것을 권장한다. 실패 지점이 넷(터널 URL 변동,
서명 검증, 3초 제한, 봇 스코프)이나 되고 전부 프로젝트 본질과 무관하다.

핵심 규칙 세 가지:
  1) ack() 를 3초 안에 가장 먼저 호출한다
  2) 버튼 value 에는 session_id 만 담고, 나머지 상태는 DB 에서 읽는다
  3) 승인 요청은 DM 으로 보내고 채널에는 개인별 응답을 노출하지 않는다
"""
from __future__ import annotations

import logging
import os
import threading

from mediator import approval_flow
from mediator import store

log = logging.getLogger("fairmeet.slack")

try:
    from slack_bolt import App
    from slack_bolt.adapter.fastapi import SlackRequestHandler
except ImportError as exc:      # pragma: no cover
    raise ImportError(
        "slack-bolt 가 설치되어 있지 않습니다. "
        "pip install slack-bolt 하거나 웹 승인 경로를 쓰세요."
    ) from exc

# signing_secret 을 넘기면 Bolt 가 요청 서명을 자동 검증한다
app = App(token=os.environ["SLACK_BOT_TOKEN"],
          signing_secret=os.environ["SLACK_SIGNING_SECRET"])
slack_handler = SlackRequestHandler(app)


def _slot_text(session_row) -> str:
    import datetime as dt
    import json
    from common.slots import format_slot
    idx = session_row["proposed_slot"]
    if idx is None:
        return "(미정)"
    slots = json.loads(session_row["slots_json"])
    return format_slot(dt.datetime.fromisoformat(slots[idx][0]),
                       dt.datetime.fromisoformat(slots[idx][1]))


@app.command("/meet")
def handle_meet(ack, body, client, logger):
    # 공식 권장: 시간이 걸리는 작업 전에 ack() 를 즉시 호출.
    # 3초 안에 응답하지 않으면 Slack 이 timeout 으로 처리한다.
    ack("에이전트들이 협상을 시작했습니다. 결과는 개별 DM 으로 보내드립니다.")

    channel_id = body["channel_id"]
    threading.Thread(target=_negotiate_bg, args=(channel_id, client, logger),
                     daemon=True).start()


def _negotiate_bg(channel_id, client, logger):
    """3초 제한 밖에서 실제 작업을 수행한다."""
    try:
        from mediator.server import _build_transport, _broadcast, GROUP_ID
        from common.ids import new_session_id
        from common.slots import build_slots, next_monday
        from mediator.negotiation import run_negotiation
        from common import config
        from agent.profile import Profile
        from common.ids import group_pseudonym
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        transport = _build_transport()
        slots = build_slots(next_monday(), "work", 60)
        sid = new_session_id()

        parts = []
        for key in ("a", "b", "c"):
            prof = Profile.load(root / "profiles" / f"{key}.json")
            aid = group_pseudonym(config.GROUP_SECRET, channel_id, prof.user_key)
            parts.append({"agent_id": aid, "display_name": prof.display_name,
                          "email": prof.email, "is_organizer": prof.is_organizer})

        store.create_session(sid, channel_id, "work", 60, slots, parts)
        outcome = run_negotiation(sid, transport, slots, channel_id,
                                  emit=_broadcast)

        if outcome.state != "PENDING_HUMAN_APPROVAL":
            client.chat_postMessage(
                channel=channel_id,
                text=f"합의에 이르지 못했습니다 ({outcome.state}). "
                     f"직접 정해 주세요.")
            return

        _send_approval_dms(sid, client, logger)
        client.chat_postMessage(
            channel=channel_id,
            text="참가자 전원에게 승인 요청 DM 을 보냈습니다. "
                 "누가 응답했는지는 채널에 표시하지 않습니다.")
    except Exception:
        logger.exception("협상 실패")
        client.chat_postMessage(channel=channel_id,
                                text="협상 중 오류가 발생했습니다.")


def _send_approval_dms(session_id, client, logger):
    """참가자별 DM. 채널에는 아무것도 노출하지 않는다.

    버튼 value 에 제안 번호를 함께 담는다. 이전 제안의 DM 버튼을 나중에 눌러도
    새 제안에 대한 응답으로 처리되지 않고 '처리된 요청'으로 거부된다.
    """
    s = store.get_session(session_id)
    slot = _slot_text(s)
    value = f"{session_id}:{s['proposal_version']}"
    for p in store.get_participants(session_id):
        try:
            u = client.users_lookupByEmail(email=p["email"])
            client.chat_postMessage(
                channel=u["user"]["id"],
                text=f"제안된 시간: {slot}",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": f"*제안된 시간*\n{slot}\n\n"
                             f"_거절하셔도 이유를 설명할 필요가 없습니다._"}},
                    {"type": "actions", "elements": [
                        {"type": "button", "style": "primary",
                         "text": {"type": "plain_text", "text": "동의"},
                         "action_id": "fm_approve", "value": value},
                        {"type": "button",
                         "text": {"type": "plain_text", "text": "거절"},
                         "action_id": "fm_reject", "value": value},
                    ]},
                ])
        except Exception:
            logger.exception("DM 실패: %s", p["email"])


def _decide(ack, body, client, verdict):
    ack()                                   # 무조건 먼저

    session_id, _, ver = body["actions"][0]["value"].partition(":")
    try:
        proposal_version = int(ver)
    except ValueError:
        proposal_version = -1                 # 형식이 다른 옛 버튼 → 무효로 처리
    slack_user = body["user"]["id"]

    info = client.users_info(user=slack_user)
    email = info["user"]["profile"].get("email")

    agent_id = None
    for p in store.get_participants(session_id):
        if p["email"] == email:
            agent_id = p["agent_id"]
            break
    if agent_id is None:
        client.chat_postEphemeral(channel=slack_user, user=slack_user,
                                  text="이 협상의 참가자가 아닙니다.")
        return

    # 승인/거절 처리와 다음 후보로의 재제안은 approval_flow 가 한 트랜잭션으로 수행한다.
    result = approval_flow.decide_for_agent(session_id, agent_id, verdict,
                                            proposal_version=proposal_version)

    if result.status == "invalid":
        msg = "만료되었거나 이미 처리된 요청입니다."
    elif result.status == "all_approved":
        from mediator.server import schedule_finalize
        schedule_finalize(session_id)
        msg = "전원 동의. 캘린더를 다시 확인한 뒤 등록합니다."
    elif result.status == "reopened":
        # 새 제안에는 새 DM(새 버튼)을 보낸다. 이전 DM 의 버튼은 위 검사로 무효다.
        _send_approval_dms(session_id, client, logger)
        msg = ("거절이 접수되었습니다. 이유는 공유되지 않습니다. "
               "다른 시간을 찾아 새로 요청합니다.")
    elif result.status == "terminated":
        msg = "거절이 접수되었습니다. 더 제안할 시간이 없어 이번 협상은 종료되었습니다."
    else:
        msg = "동의가 기록되었습니다. 다른 분들의 응답을 기다립니다."

    # 원본 DM 을 갱신 — 본인에게만 보인다
    client.chat_update(channel=body["channel"]["id"],
                       ts=body["message"]["ts"], text=msg, blocks=[])


@app.action("fm_approve")
def on_approve(ack, body, client):
    _decide(ack, body, client, "approve")


@app.action("fm_reject")
def on_reject(ack, body, client):
    _decide(ack, body, client, "reject")
