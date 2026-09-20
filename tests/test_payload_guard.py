"""payload 허용 필드 검증 테스트.

이 테스트가 보장하는 것은 '명시된 민감 필드가 전송되지 않는다' 하나뿐이다.
비용 벡터 자체가 메타데이터라는 한계는 여전히 남으므로
'privacy leak = 0' 같은 주장은 하지 않는다.
"""
from mediator.payload_guard import (
    PayloadViolation, count_free_text_fields, validate_cost_payload,
    validate_response_payload,
)

BASE = {
    "protocol": "fairmeet-negotiation/0.1",
    "method": "negotiation/costs",
    "sessionId": "a" * 32,
    "agentId": "grp7a3f_0123456789ab",
    "nonce": "0123456789abcdef",
    "issuedAt": "2026-09-19T03:00:00Z",
    "costs": [10.0, None],
    "vetoMask": [False, True],
    "scaleVersion": "cost-rules-v1",
}

RESP = {
    "protocol": "fairmeet-negotiation/0.1",
    "method": "negotiation/respond",
    "sessionId": "a" * 32,
    "agentId": "grp7a3f_0123456789ab",
    "round": 1,
    "verdict": "counter",
    "counterIndices": [0, 1],
}

SENSITIVE_FIELDS = ["eventTitle", "calendarId", "reason", "notes", "location",
                    "attendees", "description", "summary", "email", "userName"]


def _expect_violation(payload, n_slots=2, fn=validate_cost_payload):
    try:
        fn(payload, n_slots)
    except PayloadViolation:
        return
    raise AssertionError(f"통과하면 안 되는 payload 가 통과함: {payload}")


def test_valid_cost_payload_passes():
    validate_cost_payload(dict(BASE), 2)


def test_sensitive_fields_are_rejected():
    for field in SENSITIVE_FIELDS:
        p = dict(BASE)
        p[field] = "치과 진료"
        _expect_violation(p)


def test_missing_field_rejected():
    p = dict(BASE)
    del p["nonce"]
    _expect_violation(p)


def test_veto_must_have_null_cost():
    _expect_violation(dict(BASE, costs=[10.0, 99.0], vetoMask=[False, True]))


def test_non_veto_must_have_numeric_cost():
    _expect_violation(dict(BASE, costs=[None, None], vetoMask=[False, True]))


def test_cost_out_of_range_rejected():
    _expect_violation(dict(BASE, costs=[500.0, None]))
    _expect_violation(dict(BASE, costs=[-1.0, None]))


def test_bool_is_not_a_valid_cost():
    _expect_violation(dict(BASE, costs=[True, None]))


def test_slot_length_mismatch_rejected():
    _expect_violation(dict(BASE), n_slots=5)


def test_malformed_session_id_rejected():
    _expect_violation(dict(BASE, sessionId="not-a-hex-id"))


def test_malformed_agent_id_rejected():
    _expect_violation(dict(BASE, agentId="john@example.com"))


def test_protocol_version_checked():
    _expect_violation(dict(BASE, protocol="something-else/1.0"))


def test_no_free_text_fields_in_valid_payload():
    """시연 중 터미널에 찍어 보여줄 수 있는 감사 지표."""
    assert count_free_text_fields(BASE) == 0


def test_free_text_field_is_detected():
    assert count_free_text_fields(dict(BASE, reason="치과")) == 1


# ---- 응답 payload -----------------------------------------------------------

def test_valid_response_passes():
    validate_response_payload(dict(RESP), 10)


def test_response_rejects_reason_field():
    """거절 사유를 담을 통로가 존재하지 않아야 한다."""
    _expect_violation(dict(RESP, reason="돌봄"), 10, validate_response_payload)


def test_bad_verdict_rejected():
    _expect_violation(dict(RESP, verdict="maybe"), 10, validate_response_payload)


def test_too_many_counter_indices_rejected():
    """역제안을 많이 받으면 비용 벡터를 역추론할 수 있다."""
    _expect_violation(dict(RESP, counterIndices=list(range(20))),
                      50, validate_response_payload)


def test_counter_index_out_of_range_rejected():
    _expect_violation(dict(RESP, counterIndices=[99]), 10, validate_response_payload)
