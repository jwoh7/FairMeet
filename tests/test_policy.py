"""승인 정책(전원 / 최소 인원 / 최소 비율 + 필수 참석자)의 순수 규칙 테스트."""
import itertools

from mediator import policy as pm
from mediator.policy import ALL_APPROVE, ApprovalPolicy, PolicyError, build_policy, evaluate

A, B, C, D = "a", "b", "c", "d"
P3 = [A, B, C]
P4 = [A, B, C, D]


def _err(fn):
    try:
        fn()
    except PolicyError as exc:
        return str(exc)
    raise AssertionError("PolicyError 가 나야 한다")


# ---- 만들기·검증 -----------------------------------------------------------------

def test_default_is_all_approve_and_needs_everyone():
    assert ALL_APPROVE.mode == "all" and ALL_APPROVE.needed(3) == 3
    assert build_policy("all", participant_ids=P3) is ALL_APPROVE
    assert evaluate(ALL_APPROVE, P3, {A, B}, set()) == "pending"
    assert evaluate(ALL_APPROVE, P3, {A, B, C}, set()) == "approved"
    assert evaluate(ALL_APPROVE, P3, {A, B}, {C}) == "failed"


def test_organizer_is_always_required_unless_everyone_is():
    p = build_policy("min_count", min_count=2, participant_ids=P3, organizer_id=A)
    assert p.required == (A,)
    p = build_policy("min_count", min_count=2, required=[C], participant_ids=P3,
                     organizer_id=A)
    assert set(p.required) == {A, C}


def test_needed_counts_for_count_and_ratio():
    assert build_policy("min_count", min_count=2, participant_ids=P3).needed(3) == 2
    r = lambda x: build_policy("min_ratio", min_ratio=x, participant_ids=P3).needed(3)
    assert r(0.34) == 2 and r(0.5) == 2 and r(0.67) == 3 and r(1.0) == 3 and r(0.01) == 1
    assert build_policy("min_ratio", min_ratio=0.5, participant_ids=P4).needed(4) == 2
    assert build_policy("min_ratio", min_ratio=0.75, participant_ids=P4).needed(4) == 3


def test_invalid_policies_are_rejected_with_a_readable_reason():
    assert "승인 방식" in _err(lambda: build_policy("majority", participant_ids=P3))
    assert "1~3" in _err(lambda: build_policy("min_count", min_count=0, participant_ids=P3))
    assert "1~3" in _err(lambda: build_policy("min_count", min_count=4, participant_ids=P3))
    assert "정수" in _err(lambda: build_policy("min_count", min_count=1.5, participant_ids=P3))
    assert "정수" in _err(lambda: build_policy("min_count", min_count=True, participant_ids=P3))
    for bad in (0, -0.1, 1.5, "abc", None):
        assert "비율" in _err(lambda bad=bad: build_policy(
            "min_ratio", min_ratio=bad, participant_ids=P3))
    assert "참가자 목록" in _err(lambda: build_policy(
        "min_count", min_count=2, required=["zzz"], participant_ids=P3))
    # 필수 참석자가 정족수보다 많으면 기준이 모순이다
    assert "필수 참석자" in _err(lambda: build_policy(
        "min_count", min_count=1, required=[B], participant_ids=P3, organizer_id=A))
    assert "참가자가 없습니다" in _err(lambda: build_policy("min_count", min_count=1,
                                                       participant_ids=[]))


# ---- 판정 -------------------------------------------------------------------------

def test_quorum_without_required_beyond_organizer():
    pol = build_policy("min_count", min_count=2, participant_ids=P3, organizer_id=A)
    assert evaluate(pol, P3, {A, B}, set()) == "pending"            # 수는 충족했지만 C 가 아직 미응답
    assert evaluate(pol, P3, {A, B}, {C}) == "approved"             # 전원 응답 + 선택 참석자 거절 → 충족
    assert evaluate(pol, P3, {A}, {C}) == "pending"                 # 아직 B 가 남았다
    assert evaluate(pol, P3, {A}, {B, C}) == "failed"               # 남은 사람으로 정족수 불가
    assert evaluate(pol, P3, set(), {A}) == "failed"                # 필수(주최자) 거절 → 즉시 실패
    assert evaluate(pol, P3, {B, C}, set()) == "pending"            # 필수 A 가 아직이면 확정 안 됨


def test_required_participants_must_approve_regardless_of_count():
    pol = build_policy("min_count", min_count=2, required=[C], participant_ids=P3,
                       organizer_id=A)
    assert evaluate(pol, P3, {A, B}, set()) == "pending"            # 수는 충족, C 가 필수
    assert evaluate(pol, P3, {A, B, C}, set()) == "approved"
    assert evaluate(pol, P3, {A, B}, {C}) == "failed"               # 필수 거절
    assert evaluate(pol, P3, {A, C}, {B}) == "approved"


def test_ratio_policy_counts_like_a_fixed_count():
    pol = build_policy("min_ratio", min_ratio=0.5, participant_ids=P4, organizer_id=A)
    assert pol.needed(4) == 2
    assert evaluate(pol, P4, {A, D}, {B, C}) == "approved"
    assert evaluate(pol, P4, {A}, {B, C}) == "pending"              # D 가 승인하면 2명
    assert evaluate(pol, P4, {A}, {B, C, D}) == "failed"


def _brute(policy, pids, approved, rejected):
    """정의 그대로 (evaluate 와 독립적으로 다시 쓴 기준):

      failed   남은 사람이 전부 승인해도 기준을 채울 수 없다 (필수 참석자 거절 포함)
      approved 전원이 응답을 마쳤고 그 응답이 기준을 채운다
      pending  그 밖 — 기준을 이미 채웠어도 응답하지 않은 사람이 남아 있으면 기다린다
    """
    total = len(pids)
    required = set(pids) if policy.mode == "all" else set(policy.required)
    needed = policy.needed(total)
    approved, rejected = set(approved), set(rejected)

    def ok(app):
        return required <= app and len(app) >= needed

    pending = [p for p in pids if p not in approved and p not in rejected]
    best = approved | set(pending)                          # 남은 사람이 전부 승인하는 최선의 경우
    if not ok(best):
        return "failed"
    if pending:
        return "pending"
    return "approved" if ok(approved) else "failed"


def test_every_combination_matches_the_definition():
    """참가자 3~4명 × 모든 정책 × 모든 응답 조합(승인/거절/미응답)을 전수 검사한다."""
    checked = 0
    for pids in (P3, P4):
        total = len(pids)
        policies = [ALL_APPROVE]
        for n in range(1, total + 1):
            for req in ([], [pids[1]], [pids[1], pids[2]]):
                try:
                    policies.append(build_policy("min_count", min_count=n, required=req,
                                                 participant_ids=pids, organizer_id=pids[0]))
                except PolicyError:
                    pass
        for ratio in (0.25, 0.5, 0.67, 1.0):
            policies.append(build_policy("min_ratio", min_ratio=ratio,
                                         participant_ids=pids, organizer_id=pids[0]))
        for pol in policies:
            for states in itertools.product("ARN", repeat=total):     # 승인/거절/미응답
                approved = {p for p, s in zip(pids, states) if s == "A"}
                rejected = {p for p, s in zip(pids, states) if s == "R"}
                assert evaluate(pol, pids, approved, rejected) == \
                    _brute(pol, pids, approved, rejected), (pol, states)
                checked += 1
    assert checked > 1000


def test_impossible_quorum_is_detected_before_everyone_answers():
    """거절이 쌓여 남은 사람이 모두 승인해도 못 채우면, 미응답자가 있어도 즉시 failed."""
    pol = build_policy("min_count", min_count=3, participant_ids=P4, organizer_id=A)
    assert evaluate(pol, P4, {A}, {B}) == "pending"                 # C, D 가 승인하면 3명
    assert evaluate(pol, P4, {A}, {B, C}) == "failed"               # D 가 남았지만 최대 2명


# ---- 저장/복원, 문장 ---------------------------------------------------------------

def test_policy_round_trips_through_a_session_row():
    pol = build_policy("min_count", min_count=2, required=[C], participant_ids=P3,
                       organizer_id=A)
    row = {"approval_policy_json": pol.to_json(),
           "required_agents_json": '["a", "c"]'}
    got = ApprovalPolicy.from_row(row)
    assert got == pol and got.needed(3) == 2

    assert ApprovalPolicy.from_row({}) is ALL_APPROVE                          # 컬럼 없음
    assert ApprovalPolicy.from_row({"approval_policy_json": None,
                                    "required_agents_json": None}) is ALL_APPROVE
    assert ApprovalPolicy.from_row({"approval_policy_json": '{"mode": "weird"}',
                                    "required_agents_json": "[]"}) is ALL_APPROVE
    assert ApprovalPolicy.from_row({"approval_policy_json": "not json",
                                    "required_agents_json": "[]"}) is ALL_APPROVE


def test_descriptions_state_the_rule_and_never_mention_who_declined():
    names = {A: "민지", B: "준호", C: "서연"}
    pol = build_policy("min_count", min_count=2, required=[C], participant_ids=P3,
                       organizer_id=A)
    text = pm.describe(pol, 3, names)
    assert "민지" in text and "서연" in text and "2명" in text and "준호" not in text
    assert "거절" in text                                     # 선택 참석자가 거절해도 …
    assert pm.describe(ALL_APPROVE, 3, names).startswith("참가자 전원")
    assert "필수 참석자입니다" in pm.role_text(pol, C)
    assert "선택 참석자" in pm.role_text(pol, B)
    assert "전원 동의" in pm.role_text(ALL_APPROVE, B)
