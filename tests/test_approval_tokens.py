"""승인 링크 토큰: 서명, 만료, 세션·제안 버전·참가자 결합, 1회용."""
import time

from common import config
from mediator import approval_flow, store, tokens
from tests.flow_helpers import NAMES, fresh_db, start_flow

SID = "a" * 32
AID = "grpaaaa_000000000001"


def setup_function(fn=None):
    fresh_db()


def _mint(**over):
    args = dict(session_id=SID, proposal_version=1, agent_id=AID,
                expires_at=int(time.time()) + 600)
    args.update(over)
    return tokens.mint(**args)


def _expect_invalid(token, now=None):
    try:
        tokens.verify(token, now)
    except tokens.TokenInvalid:
        return
    raise AssertionError("잘못된 토큰이 검증을 통과함")


def test_valid_token_roundtrip_carries_session_version_and_participant():
    token, jti = _mint(proposal_version=3)
    c = tokens.verify(token)
    assert (c.session_id, c.proposal_version, c.agent_id, c.jti) == (SID, 3, AID, jti)


def test_tampered_payload_or_signature_is_rejected():
    token, _ = _mint()
    body, sig = token.split(".")
    other, _ = _mint(proposal_version=2)
    _expect_invalid(other.split(".")[0] + "." + sig)      # 다른 버전의 payload + 기존 서명
    _expect_invalid(body + "." + sig[:-2] + "AA")
    _expect_invalid(body)                                  # 서명 없음
    _expect_invalid("")
    _expect_invalid("가.나")
    _expect_invalid("x" * 5000)


def test_expired_token_is_rejected():
    token, _ = _mint(expires_at=int(time.time()) + 60)
    tokens.verify(token)
    _expect_invalid(token, now=time.time() + 61)


def test_token_signed_with_another_secret_is_rejected():
    token, _ = _mint()
    old = config.GROUP_SECRET
    config.GROUP_SECRET = old + "-rotated"
    try:
        _expect_invalid(token)
    finally:
        config.GROUP_SECRET = old
    tokens.verify(token)


def test_each_participant_gets_a_different_link_and_jti():
    f = start_flow()
    toks = {n: f.token_for(n) for n in NAMES}
    assert len(set(toks.values())) == 3
    claims = {n: tokens.verify(t) for n, t in toks.items()}
    assert len({c.jti for c in claims.values()}) == 3
    assert len({c.agent_id for c in claims.values()}) == 3
    assert {c.session_id for c in claims.values()} == {f.sid}
    assert {c.proposal_version for c in claims.values()} == {1}


def test_token_is_bound_to_its_own_participant():
    """민지의 토큰의 agent_id 를 서연 것으로 바꿔도(서명 불일치) 통과하지 못한다."""
    f = start_flow()
    a, c = f.token_for("민지"), f.token_for("서연")
    forged = c.split(".")[0] + "." + a.split(".")[1]
    assert f.approve("민지", token=forged).status == "invalid"
    assert store.approval_tally(f.sid) == (0, 0, 3)


def test_database_never_stores_the_token_itself():
    f = start_flow()
    toks = [f.token_for(n) for n in NAMES]
    with store.connect() as c:
        dump = []
        for t in ("approval_tokens", "notifications", "participants", "sessions",
                  "approvals", "proposals", "transitions"):
            dump += [tuple(r) for r in c.execute(f"SELECT * FROM {t}")]
    blob = repr(dump)
    for t in toks:
        assert t not in blob and t.split(".")[1] not in blob
    # jti 만 저장되고, 그것만으로는 링크를 만들 수 없다
    jtis = {tokens.verify(t).jti for t in toks}
    with store.connect() as c:
        stored = {r["jti"] for r in c.execute("SELECT jti FROM approval_tokens")}
    assert jtis == stored


def test_token_is_single_use():
    f = start_flow()
    t = f.token_for("민지")
    assert approval_flow.inspect_token(t) is not None
    assert f.approve("민지").status == "recorded"
    assert approval_flow.inspect_token(t) is None          # 사용한 링크는 화면도 열리지 않는다
    assert f.approve("민지", token=t).status == "invalid"
    assert f.reject("민지", token=t).status == "invalid"


def test_expired_link_is_rejected_by_the_flow():
    f = start_flow()
    t = f.token_for("민지")
    assert approval_flow.inspect_token(t, now=time.time() + 24 * 3600) is None
    assert approval_flow.decide_with_token(t, "approve", now=time.time() + 24 * 3600
                                           ).status == "invalid"
    assert store.approval_tally(f.sid) == (0, 0, 3)
