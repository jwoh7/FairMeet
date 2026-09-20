"""FairMeet 전체 흐름 오프라인 데모.

Google 계정, OAuth, 네트워크 없이 협상부터 캘린더 커밋까지 전 구간을 실행한다.
발표 당일 Wi-Fi가 죽거나 토큰이 만료되어도 이 경로로 시연을 마칠 수 있다.

사용법:
    python -m eval.demo_local                  # 기본 시나리오
    python -m eval.demo_local --seed-balance   # 과거 양보 이력을 주입
    python -m eval.demo_local --stale          # 승인 중 일정 충돌 주입
    python -m eval.demo_local --fail-calendar  # 캘린더 조회 실패 주입
    python -m eval.demo_local --reject         # 참가자 1명 거절
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

from agent.calendar_client import FakeCalendar
from agent.calendar_settings import MemorySettings
from agent.negotiator import LocalAgent
from agent.profile import Profile
from common import config
from common.ids import new_session_id
from common.slots import build_slots, format_slot, next_monday
from mediator import approval_flow
from mediator import commit as commit_mod
from mediator import store
from mediator.negotiation import run_negotiation
from mediator.notifier import FakeNotifier
from mediator.transport import InProcessTransport

ROOT = Path(__file__).resolve().parent.parent
GROUP_ID = "demo-team"

C = {
    "dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "y": "\033[33m",
    "r": "\033[31m", "c": "\033[36m", "x": "\033[0m",
}


def hr(title: str = "") -> None:
    if title:
        print(f"\n{C['b']}{'─' * 4} {title} {'─' * (62 - len(title))}{C['x']}")
    else:
        print(f"{C['dim']}{'─' * 68}{C['x']}")


def build_agents(fake_spec: dict, fail_for: str | None = None) -> dict:
    agents = {}
    for key in ("a", "b", "c"):
        prof = Profile.load(ROOT / "profiles" / f"{key}.json")
        backend = FakeCalendar(
            prof.display_name,
            fake_spec[key]["busy"],
            fail_fetch=(fail_for == key),
            events=fake_spec[key].get("events"),
        )
        agent = LocalAgent(prof, backend, config.GROUP_SECRET, GROUP_ID,
                           settings_store=MemorySettings(fake_spec[key].get("colorSettings")))
        agents[agent.agent_id] = agent
    return agents


def print_event(ev: dict) -> None:
    t = ev["type"]
    if t == "costs_collected":
        print(f"  {C['c']}▸{C['x']} 비용 수집 완료 — 에이전트 {ev['agents']}명, "
              f"슬롯 {ev['slots']}개, 규칙 {ev['scale']}")
        print(f"    {C['dim']}중재자가 받은 자유 텍스트 필드: "
              f"{ev['free_text_fields']}개{C['x']}")
    elif t == "feasible":
        print(f"  {C['c']}▸{C['x']} 전원 가능한 공통 슬롯: {ev['common_slots']}개")
    elif t == "balance_loaded":
        print(f"  {C['y']}▸{C['x']} 과거 양보 잔액 반영: {ev['balance']}")
        print(f"    {C['dim']}양수 = 과거에 더 희생 → 이번엔 배려 대상{C['x']}")
    elif t == "propose":
        print(f"  {C['b']}[라운드 {ev['round']}]{C['x']} 제안 → {ev['slot']}")
        print(f"    {C['dim']}탐색 {ev['searched']}개 / 최대보정불이익 "
              f"{ev['max_adjusted']} / regret {ev['regrets']}{C['x']}")
    elif t == "responses":
        for aid, v in ev["verdicts"]:
            mark = {"accept": f"{C['g']}수락{C['x']}",
                    "counter": f"{C['y']}역제안{C['x']}",
                    "reject": f"{C['r']}거부{C['x']}"}[v]
            print(f"    {aid[:16]}… {mark}")
        if ev["counter_indices"]:
            print(f"    {C['dim']}역제안 슬롯 {ev['counter_indices']} "
                  f"→ 다음 라운드 우선 탐색{C['x']}")
    elif t == "agreed":
        print(f"  {C['g']}▸ 에이전트 합의{C['x']} — {ev['slot']}")
        print(f"    {C['dim']}이번에 가장 손해 본 사람: {ev['worst_off'][:16]}… "
              f"(공개 채널에는 표시하지 않음){C['x']}")
    elif t == "rechecking":
        print(f"  {C['c']}▸{C['x']} 커밋 직전 전원 캘린더 재확인 중…")
    elif t == "recheck_clear":
        print(f"  {C['g']}▸{C['x']} 충돌 없음")
    elif t == "stale_conflict":
        print(f"  {C['r']}▸ STALE_CONFLICT{C['x']} — {ev['who']}님 캘린더가 변경됨. "
              f"이벤트를 만들지 않습니다")
    elif t == "committed":
        print(f"  {C['g']}▸ 커밋 완료{C['x']} — {ev['slot']}")
        print(f"    이벤트 ID {ev['event_id'][:20]}… / "
              f"신규생성={ev['newly_created']} / organizer={ev['organizer']} / "
              f"초대 {ev['attendees']}명")
        print(f"    {C['dim']}잔액 갱신: {ev['balance_applied']} "
              f"(커밋 성공 후 정확히 1회){C['x']}")
    elif t in ("no_feasible_slot", "max_rounds_exceeded"):
        print(f"  {C['r']}▸ {t}{C['x']} {ev.get('detail', '')}")
    elif t == "calendar_unavailable":
        print(f"  {C['r']}▸ 캘린더 조회 실패{C['x']} — "
              f"{ev.get('agent') or ev.get('who')}")
        print(f"    {C['dim']}fail-closed: 빈 시간으로 처리하지 않고 사람에게 넘깁니다{C['x']}")
    elif t == "agent_unreachable":
        print(f"  {C['r']}▸ 에이전트 응답 없음{C['x']} — {ev.get('agent')}")
    else:
        print(f"  ▸ {t} {ev}")


def main() -> int:
    if sys.platform == "win32":
        # Windows 콘솔의 기본 코드페이지(예: CP949)는 –, —, ▸ 등을 표현하지
        # 못해 UnicodeEncodeError로 죽는다. 표준출력/에러를 UTF-8로 강제한다.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (AttributeError, ValueError):
                pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--seed-balance", action="store_true",
                    help="과거 양보 이력을 원장에 주입")
    ap.add_argument("--stale", action="store_true",
                    help="승인 대기 중 일정 충돌을 주입")
    ap.add_argument("--fail-calendar", action="store_true",
                    help="캘린더 조회 실패를 주입")
    ap.add_argument("--reject", action="store_true",
                    help="참가자 1명이 거절")
    ap.add_argument("--keep-db", action="store_true",
                    help="임시 DB를 지우지 않음")
    args = ap.parse_args()

    db = tempfile.mktemp(suffix=".db")
    store.set_db_path(db)
    store.init_db()

    fake_spec = json.loads(
        (ROOT / "profiles" / "fake_calendars.json").read_text(encoding="utf-8"))
    agents = build_agents(fake_spec, fail_for="b" if args.fail_calendar else None)
    transport = InProcessTransport(agents)

    print(f"{C['b']}FairMeet 오프라인 데모{C['x']}  "
          f"{C['dim']}(Google 계정·네트워크 없이 전 구간 실행){C['x']}")
    hr("참가자")
    for aid, ag in sorted(agents.items()):
        p = ag.profile
        role = " (organizer)" if p.is_organizer else ""
        print(f"  {p.display_name}{role}")
        kinds = ag.settings_store.load().colors
        print(f"    {C['dim']}가명 {aid} / 일정 색상 분류 "
              f"{"설정됨 " + str(len(kinds)) + "개" if kinds else "미설정(모든 일정 고정)"}{C['x']}")
    print(f"\n  {C['dim']}중재자는 위 가명과 숫자 비용만 봅니다. "
          f"일정 제목·사유는 에이전트 밖으로 나가지 않습니다.{C['x']}")

    if args.seed_balance:
        ids = sorted(agents)
        store.seed_balance(GROUP_ID, {ids[0]: 22.0, ids[1]: -11.0, ids[2]: -11.0})
        hr("과거 이력 주입")
        print(f"  {sorted(agents)[0][:16]}… 이 지난 협상에서 연속으로 양보한 상태")

    slots = build_slots(next_monday(dt.date(2026, 9, 19)), "work", 60)
    session_id = new_session_id()
    store.create_session(
        session_id, GROUP_ID, "work", 60, slots,
        [{"agent_id": aid, "display_name": ag.profile.display_name,
          "email": ag.profile.email, "is_organizer": ag.profile.is_organizer}
         for aid, ag in agents.items()])

    hr("협상")
    print(f"  탐색 윈도우: 다음 주 평일 09–19시, 60분 회의, 총 {len(slots)}개 슬롯\n")
    outcome = run_negotiation(session_id, transport, slots, GROUP_ID,
                              lam=config.LAMBDA_WORK, emit=print_event)

    if outcome.state != "PENDING_HUMAN_APPROVAL":
        hr("결과")
        print(f"  {C['r']}{outcome.state}{C['x']} — {outcome.reason}")
        print(f"  {C['dim']}이 경로도 정상 동작입니다. 사람이 직접 정하도록 넘깁니다.{C['x']}")
        if not args.keep_db:
            Path(db).unlink(missing_ok=True)
        return 0

    # ---- 인간 승인 ---------------------------------------------------------
    hr("승인 (참가자별 비공개)")
    parts = store.get_participants(session_id)
    # 데모는 실제 메일을 보내지 않는다 (FakeNotifier). 링크는 화면에 찍지 않는다.
    notifier = FakeNotifier()
    approval_flow.dispatch_notifications(session_id, notifier)
    for m in notifier.outbox:
        print(f"  {m.to_name} ← 승인 요청 메일 발송 "
              f"{C['dim']}(개인 링크: 서명·1회용·만료){C['x']}")
    print(f"  {C['dim']}각자 개인 링크로 응답합니다. 누가 아직 응답하지 않았는지는 "
          f"아무에게도 표시되지 않습니다.{C['x']}\n")

    if args.reject:
        p = parts[1]
        first_slot = store.get_session(session_id)["proposed_slot"]
        res = approval_flow.decide_for_agent(session_id, p["agent_id"], "reject")
        print(f"  {C['r']}▸ 1순위 제안이 거절됨{C['x']} "
              f"— 이유도, 누가 거절했는지도 다른 참가자에게 전달되지 않습니다")
        if res.status != "reopened":
            hr("결과")
            print(f"  {C['y']}{res.state}{C['x']} — 더 제안할 수 있는 시간이 없습니다")
            if not args.keep_db:
                Path(db).unlink(missing_ok=True)
            return 0
        second = store.get_session(session_id)["proposed_slot"]
        s2, e2 = slots[second]
        print(f"  {C['c']}▸ 2순위 재제안{C['x']} {format_slot(s2, e2)} "
              f"{C['dim']}(이전 슬롯 {first_slot}번은 다시 제안되지 않음, "
              f"이전 링크·승인은 무효, 새 이메일 발송){C['x']}\n")
        approval_flow.dispatch_notifications(session_id, notifier)

    for p in parts:
        r1 = approval_flow.decide_for_agent(session_id, p["agent_id"], "approve")
        r2 = approval_flow.decide_for_agent(session_id, p["agent_id"], "approve")  # 중복 클릭
        ok, no, total = store.approval_tally(session_id)
        print(f"  {C['g']}✓{C['x']} {p['display_name']} 동의 "
              f"{C['dim']}(중복 클릭 시도: {r2.status}) — {ok}/{total}{C['x']}")

    if args.stale:
        victim = parts[2]
        ag = agents[victim["agent_id"]]
        cur_slot = store.get_session(session_id)["proposed_slot"]
        s, e = slots[cur_slot]
        ag.backend.add_busy("primary", s, e)
        print(f"\n  {C['y']}▸ 장애 주입{C['x']} — 승인 완료 후 "
              f"{victim['display_name']}님 캘린더에 일정이 새로 생김")

    hr("재확인 및 커밋")
    result = commit_mod.finalize(session_id, transport, emit=print_event)

    # ---- 결과 ---------------------------------------------------------------
    hr("결과")
    if result.state == "COMMITTED":
        print(f"  {C['g']}{C['b']}COMMITTED{C['x']} — {result.slot_text}")
        org = next(p for p in parts if p["is_organizer"])
        ag = agents[org["agent_id"]]
        ev = list(ag.backend.created_events.values())[0]
        print(f"\n  organizer({org['display_name']}) 캘린더에 생성된 이벤트:")
        print(f"    제목     {ev['summary']}")
        print(f"    시각     {ev['start'][:16]} – {ev['end'][11:16]}")
        print(f"    참석자   {', '.join(ev['attendees'])}")
        print(f"    Meet     {ev['meetLink']}")
        total_events = sum(len(a.backend.created_events) for a in agents.values())
        mark = C['g'] if total_events == 1 else C['r']
        print(f"\n  {mark}전체 캘린더에 생성된 이벤트 총 개수: {total_events}{C['x']} "
              f"{C['dim']}(1이어야 정상 — 인원수만큼 생기면 버그){C['x']}")
    else:
        print(f"  {C['y']}{result.state}{C['x']} — {result.reason}")

    hr("양보 잔액 원장")
    bal = store.get_balance(GROUP_ID, sorted(agents))
    for aid in sorted(agents):
        name = agents[aid].profile.display_name
        v = bal[aid]
        bar = "█" * int(abs(v) / 2)
        side = "배려 대상" if v > 0.5 else ("양보할 차례" if v < -0.5 else "균형")
        col = C['y'] if v > 0.5 else (C['c'] if v < -0.5 else C['dim'])
        print(f"  {name:<4} {col}{v:+6.1f}{C['x']} {col}{bar}{C['x']}  {C['dim']}{side}{C['x']}")
    print(f"\n  {C['dim']}합계 {sum(bal.values()):+.2f} "
          f"(decay 제외하면 항상 0 — zero-sum){C['x']}")

    if not args.keep_db:
        Path(db).unlink(missing_ok=True)
    else:
        print(f"\n  DB: {db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
