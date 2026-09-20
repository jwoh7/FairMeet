"""Mediator 가 받는 payload 에 자유 텍스트가 섞이지 않는지 강제한다.

이 모듈이 보장하는 것과 보장하지 않는 것을 정확히 구분할 것:

  보장한다:
    일정 제목, 설명, 위치, 개인 사유, 원본 캘린더 ID 같은 명시된 민감
    필드가 중재자에게 전송되지 않는다. 허용 키 목록 밖의 필드가 하나라도
    있으면 요청 자체를 거부한다.

  보장하지 않는다:
    비용 벡터 자체가 생활 패턴을 추론할 수 있는 메타데이터라는 점.
    매주 같은 시간이 거부권이면 정기 일정의 존재가 드러나고, 평일 오전이
    전부 고비용이면 근무 형태를 짐작할 수 있다.

따라서 'privacy leak = 0' 같은 지표는 쓰지 않는다. 정확한 표현은
'노출되는 정보의 양을 최소화했다' 이다.
"""
from __future__ import annotations

import re

ALLOWED_COST_KEYS = frozenset({
    "protocol", "method", "sessionId", "agentId", "nonce",
    "issuedAt", "costs", "vetoMask", "scaleVersion",
})

ALLOWED_RESPONSE_KEYS = frozenset({
    "protocol", "method", "sessionId", "agentId", "round",
    "verdict", "counterIndices",
})

PATTERNS = {
    "protocol":     re.compile(r"^fairmeet-negotiation/\d+\.\d+$"),
    "method":       re.compile(r"^negotiation/[a-z]+$"),
    "sessionId":    re.compile(r"^[0-9a-f]{32}$"),
    "agentId":      re.compile(r"^grp[0-9a-f]{4}_[0-9a-f]{12}$"),
    "nonce":        re.compile(r"^[0-9a-f]{16,32}$"),
    "issuedAt":     re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+(Z|[+-]\d{2}:\d{2})$"),
    "scaleVersion": re.compile(r"^cost-rules-v\d+$"),
}

MAX_COST = 100.0
MAX_COUNTER_INDICES = 5


class PayloadViolation(ValueError):
    """허용되지 않은 형태의 payload. 중재자는 400으로 거부한다."""


def _check_strings(payload: dict, keys) -> None:
    for key in keys:
        pat = PATTERNS.get(key)
        if pat is None:
            continue
        val = payload.get(key)
        if not isinstance(val, str) or not pat.fullmatch(val):
            raise PayloadViolation(f"{key} 형식 위반: {val!r}")


def validate_cost_payload(payload: dict, n_slots: int) -> None:
    """비용 벡터 payload 검증. 위반 시 PayloadViolation."""
    if not isinstance(payload, dict):
        raise PayloadViolation("payload 가 객체가 아님")

    extra = set(payload) - ALLOWED_COST_KEYS
    if extra:
        raise PayloadViolation(f"허용되지 않은 필드: {sorted(extra)}")
    missing = ALLOWED_COST_KEYS - set(payload)
    if missing:
        raise PayloadViolation(f"누락된 필드: {sorted(missing)}")

    _check_strings(payload, ALLOWED_COST_KEYS)

    costs, veto = payload["costs"], payload["vetoMask"]
    if not isinstance(costs, list) or len(costs) != n_slots:
        raise PayloadViolation(f"costs 길이가 {n_slots} 이 아님 (받은 값: "
                               f"{len(costs) if isinstance(costs, list) else type(costs)})")
    if not isinstance(veto, list) or len(veto) != n_slots:
        raise PayloadViolation(f"vetoMask 길이가 {n_slots} 이 아님")

    for i, (c, v) in enumerate(zip(costs, veto)):
        if not isinstance(v, bool):
            raise PayloadViolation(f"vetoMask[{i}] 가 bool 이 아님")
        if v:
            if c is not None:
                raise PayloadViolation(f"veto 슬롯 {i} 의 cost 가 null 이 아님")
        else:
            if isinstance(c, bool) or not isinstance(c, (int, float)):
                raise PayloadViolation(f"costs[{i}] 가 숫자가 아님")
            if not (0.0 <= float(c) <= MAX_COST):
                raise PayloadViolation(f"costs[{i}]={c} 가 [0,{MAX_COST}] 범위 밖")


def validate_response_payload(payload: dict, n_slots: int) -> None:
    """제안 응답 payload 검증."""
    if not isinstance(payload, dict):
        raise PayloadViolation("payload 가 객체가 아님")

    extra = set(payload) - ALLOWED_RESPONSE_KEYS
    if extra:
        raise PayloadViolation(f"허용되지 않은 필드: {sorted(extra)}")

    _check_strings(payload, ALLOWED_RESPONSE_KEYS)

    if payload.get("verdict") not in ("accept", "counter", "reject"):
        raise PayloadViolation(f"잘못된 verdict: {payload.get('verdict')!r}")

    ci = payload.get("counterIndices", [])
    if not isinstance(ci, list):
        raise PayloadViolation("counterIndices 가 배열이 아님")
    if len(ci) > MAX_COUNTER_INDICES:
        raise PayloadViolation(
            f"counterIndices 가 {MAX_COUNTER_INDICES} 개를 초과 — "
            f"비용 벡터 역추론 위험")
    for x in ci:
        if isinstance(x, bool) or not isinstance(x, int):
            raise PayloadViolation(f"counterIndices 원소가 정수가 아님: {x!r}")
        if not (0 <= x < n_slots):
            raise PayloadViolation(f"counterIndices 원소가 범위 밖: {x}")


def count_free_text_fields(payload: dict) -> int:
    """감사용: 정규식으로 형식이 고정되지 않은 문자열 필드의 개수.

    0 이어야 정상이다. 시연 중 터미널에 이 값을 찍어 보여주면
    '제목이 정말 안 넘어가나요?'에 대한 즉답이 된다.
    """
    n = 0
    for k, v in payload.items():
        if isinstance(v, str) and k not in PATTERNS:
            n += 1
    return n
