"""회귀 테스트 (P1): 필수 참석자·정족수를 말하는 자연어 표현 일부가 '해석하지 못한 조건'으로
빠지던 문제.

실제 원인 (mediator/nl_rules.py):
  1. 필수 참석자 패턴이 '필수 참석자'만 봤고 '필수 참여자'(및 붙여 쓴 '필수참여자')는 못 봤다.
     또 '재원이었으면 좋겠고' 처럼 서술어가 붙으면 이름 매칭(_find_names)의 조사 경계에 걸려 실패했다.
  2. '반드시 포함해줘' 처럼 참석 동사 없이 '포함'으로 말하면 필수 참석자로 보지 않았다.
  3. 정족수 인원을 숫자로만 읽어서 '두 명', '세 명 중 두 명' 같은 우리말 수사를 못 읽었다.

'2명만 참여해도 된다'는 참가자를 2명만 초대하라는 뜻이 아니라, 3명 모두 초대하되 최소 승인
인원이 2명이라는 뜻이다. 그리고 그 2명이 모여도 P0-1 정책에 따라 전원 응답(또는 기한)까지 기다린다.

실제 이메일·Google Calendar·LLM 은 쓰지 않는다 (규칙 파서 + FakeServer).
"""
import datetime as dt
import json
import uuid

from common.slots import KST
from mediator import nl_rules, store
from tests.server_helpers import FakeServer

NOW = dt.datetime(2026, 9, 20, 10, 0, tzinfo=KST)
NAMES = ["재원", "도연", "승현"]

FULL = ("필수 참여자는 재원이었으면 좋겠고, 재원, 도연, 승현 중 2명만 참여해도 되는 "
        "90분 회의를 다음 주에 잡아줘.")


def _raw(text, names=NAMES):
    sents = nl_rules.split_sentences(nl_rules.sanitize_text(text, 1000))
    return nl_rules.rules_extract(sents, names, NOW)


def _types(raw):
    return [c["type"] for c in raw.get("constraints", [])]


def _of(raw, typ):
    return [c for c in raw.get("constraints", []) if c["type"] == typ]


def _val(c):
    v = c.get("value_json") or c.get("value")
    return json.loads(v) if isinstance(v, str) else v


def _req(fx, text):
    return fx.client.post("/api/requests", json={"text": text, "idempotencyKey": uuid.uuid4().hex})


def _count(table):
    with store.connect() as c:
        return c.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]


# ---- 규칙 파서: 표현별 해석 ---------------------------------------------------------------

def test_the_reported_sentence_is_understood_not_marked_unsupported():
    raw = _raw(FULL)
    assert "other" not in _types(raw), _types(raw)          # '해석하지 못한 조건'이 없다
    req = _of(raw, "required_participant")
    assert req and _val(req[0])["names"] == ["재원"]
    q = _of(raw, "quorum")
    assert q and _val(q[0])["kind"] == "min_count" and _val(q[0])["count"] == 2


def test_required_participant_phrasings():
    for text in ("재원은 꼭 참석해야 해",
                 "재원을 필수 참석자로 해줘",
                 "필수 참석자는 재원이야",
                 "재원은 반드시 포함해줘",
                 "필수참여자는 재원이었으면 좋겠고",
                 "필수 참여자는 재원이면 좋겠어"):
        raw = _raw(text)
        req = _of(raw, "required_participant")
        assert req, (text, _types(raw))
        assert "재원" in _val(req[0])["names"], text
        assert "other" not in _types(raw), (text, _types(raw))


def test_two_required_participants_at_once():
    raw = _raw("재원, 도연은 꼭 참석해야 해")
    req = _of(raw, "required_participant")
    assert req and set(_val(req[0])["names"]) == {"재원", "도연"}
    assert "other" not in _types(raw)


def test_quorum_phrasings_with_korean_numerals():
    for text, count in (("세 명 중 두 명만 참여해도 돼", 2),
                        ("최소 두 명이 동의하면 진행해", 2),
                        ("최소 2명이 동의하면 돼", 2),
                        ("최소 세 명이 동의해야 해", 3)):
        raw = _raw(text)
        q = _of(raw, "quorum")
        assert q, (text, _types(raw))
        v = _val(q[0])
        assert v["kind"] == "min_count" and v["count"] == count, (text, v)
        assert "other" not in _types(raw), (text, _types(raw))


def test_majority_still_maps_to_the_existing_ratio_policy():
    raw = _raw("과반수가 승인하면 확정해")
    q = _of(raw, "quorum")
    assert q and _val(q[0])["kind"] == "majority"
    assert "other" not in _types(raw)


def test_an_unknown_name_is_not_applied_silently():
    """등록되지 않은 이름은 추측해서 적용하지 않고 확인이 필요한 상태로 남긴다."""
    raw = _raw("필수 참석자는 홍길동이야")
    req = _of(raw, "required_participant")
    names = _val(req[0])["names"] if req else []
    assert "홍길동" not in NAMES
    assert not (set(names) & set(NAMES))                    # 등록된 사람을 임의로 필수로 만들지 않는다


# ---- 제품 흐름: 초안에 그대로 반영되고, 확인 전에는 아무 일도 없다 ----------------------------------

def test_the_draft_shows_required_and_quorum_and_starts_nothing():
    with FakeServer() as fx:
        j = _req(fx, FULL).json()
        assert j["status"] == "draft"
        d = j["draft"]
        s = d["summary"]
        assert s["durationMin"] == 90
        assert sorted(s["participants"]) == sorted(NAMES)   # 3명 모두 초대한다
        assert s["required"] == ["재원"]
        assert "2명" in s["approval"]
        assert d["approvalSpec"]["mode"] == "min_count" and d["approvalSpec"]["min_count"] == 2
        assert d["approvalSpec"]["needed"] == 2 and d["approvalSpec"]["total"] == 3
        # 해석하지 못한 조건으로 밀려나지 않았다
        assert [c for c in d["constraints"] if c["type"] == "other"] == []
        assert all(c["supported"] for c in d["constraints"])
        # 확인 전에는 세션·메일·이벤트가 전혀 없다
        assert _count("sessions") == 0 and not fx.notifier.outbox and fx.events() == 0


def test_required_and_quorum_can_be_edited_before_starting():
    with FakeServer() as fx:
        j = _req(fx, FULL).json()
        did = j["draftId"]
        ids = {x["name"]: x["id"] for x in j["draft"]["people"]["available"]}

        # 필수 참석자와 최소 승인 인원을 화면에서 직접 바꾼다
        r = fx.client.post(f"/api/requests/{did}/resolve", json={
            "required": [ids["도연"]],
            "approval": {"mode": "min_count", "count": 3}})
        assert r.status_code == 200, r.text
        d = r.json()["draft"]
        assert d["summary"]["required"] == ["도연"]
        assert d["approvalSpec"]["min_count"] == 3 and d["approvalSpec"]["needed"] == 3
        assert _count("sessions") == 0                      # 수정만으로는 시작하지 않는다


def test_confirming_uses_the_parsed_policy_for_the_real_session():
    with FakeServer() as fx:
        j = _req(fx, FULL).json()
        did, d = j["draftId"], j["draft"]
        # 문장이 '최소 2명에 필수 참석자가 포함되는지' 를 말하지 않았으므로 확인 질문이 뜬다 (설계된 동작).
        pending = [q for q in d["questions"] if q["pending"]]
        assert pending and "필수 참석자" in pending[0]["text"]
        payload = {"answers": {q["id"]: "total" for q in pending},
                   "ack_defaults": [x["key"] for x in d["defaults"]]}
        d = fx.client.post(f"/api/requests/{did}/resolve", json=payload).json()["draft"]
        assert d["ready"] is True, d["blocking"]
        sid = fx.client.post(f"/api/requests/{did}/confirm").json()["meetingId"]
        row = store.get_session(sid)
        assert row["approvals_needed"] == 2
        assert json.loads(row["approval_policy_json"])["mode"] == "min_count"
        names = {p["agent_id"]: p["display_name"] for p in store.get_participants(sid)}
        assert sorted(names.values()) == sorted(NAMES)       # 3명 모두 초대됐다
        assert [names[a] for a in json.loads(row["required_agents_json"])] == ["재원"]
