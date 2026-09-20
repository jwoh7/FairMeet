"""에이전트 ↔ 중재자 메시지 스키마.

설계 원칙: 민감 정보가 스며들 수 있는 '자유 텍스트 필드'를 아예 만들지 않는다.
거절 사유(reason) 필드가 존재하지 않는 것은 실수가 아니라 의도다 —
사유를 설명할 통로가 없어야 사유를 설명할 압박도 사라진다.

pydantic 이 없는 환경에서도 동작하도록 dataclass 로 쓰고,
검증은 payload_guard 가 정규식으로 수행한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Literal

PROTOCOL = "fairmeet-negotiation/0.1"
SCALE_VERSION = "cost-rules-v1"     # 모든 에이전트가 동일해야 비용이 비교 가능

MAX_COUNTER_INDICES = 5             # 더 많이 받으면 비용 벡터를 역추론할 수 있음

Verdict = Literal["accept", "counter", "reject"]


@dataclass
class AgentCard:
    """A2A 에서 영감을 받았으나 A2A 1.0 규격은 아니다.

    a2aCompliant=False 를 명시적으로 박아두는 이유:
    이 필드를 보고도 'A2A 호환'이라고 발표하면 그건 의도적 허위진술이 된다.
    """
    agent_id: str
    display_name: str = "FairMeet Personal Agent"
    protocol: str = PROTOCOL
    a2a_inspired: bool = True
    a2a_compliant: bool = False
    scale_version: str = SCALE_VERSION
    skills: list[dict] = field(default_factory=lambda: [{
        "id": "schedule.negotiate",
        "name": "Schedule negotiation",
        "description": "개인 제약을 공개하지 않고 슬롯별 비용을 산출하여 협상에 참여",
    }])
    capabilities: dict = field(default_factory=lambda: {
        "streaming": False, "counterProposal": True,
    })
    security: dict = field(default_factory=lambda: {"scheme": "Bearer"})

    def to_json(self) -> dict:
        return {
            "protocol": self.protocol,
            "a2aInspired": self.a2a_inspired,
            "a2aCompliant": self.a2a_compliant,
            "name": self.display_name,
            "agentId": self.agent_id,
            "scaleVersion": self.scale_version,
            "skills": self.skills,
            "capabilities": self.capabilities,
            "security": self.security,
        }


@dataclass
class CostVector:
    """에이전트 → 중재자. 이 객체가 신뢰 경계를 넘는 유일한 데이터다.

    costs[i] 는 float 또는 None(거부권). None 이면 veto_mask[i] 가 True 여야 한다.
    """
    session_id: str
    agent_id: str
    nonce: str
    issued_at: str
    costs: list[float | None]
    veto_mask: list[bool]
    scale_version: str = SCALE_VERSION

    def to_json(self) -> dict:
        return {
            "protocol": PROTOCOL,
            "method": "negotiation/costs",
            "sessionId": self.session_id,
            "agentId": self.agent_id,
            "nonce": self.nonce,
            "issuedAt": self.issued_at,
            "costs": self.costs,
            "vetoMask": self.veto_mask,
            "scaleVersion": self.scale_version,
        }

    @staticmethod
    def from_json(d: dict) -> "CostVector":
        return CostVector(
            session_id=d["sessionId"], agent_id=d["agentId"],
            nonce=d["nonce"], issued_at=d["issuedAt"],
            costs=d["costs"], veto_mask=d["vetoMask"],
            scale_version=d["scaleVersion"],
        )


@dataclass
class Proposal:
    """중재자 → 에이전트."""
    session_id: str
    round: int
    slot_index: int

    def to_json(self) -> dict:
        return {
            "protocol": PROTOCOL,
            "method": "negotiation/propose",
            "sessionId": self.session_id,
            "round": self.round,
            "slotIndex": self.slot_index,
        }


@dataclass
class Response:
    """에이전트 → 중재자.

    reason 필드가 없다는 점에 주목할 것. 거절에 사유를 요구하지 않는 것이
    이 시스템의 존재 이유이므로, 스키마 수준에서 통로를 막아둔다.
    """
    session_id: str
    agent_id: str
    round: int
    verdict: Verdict
    counter_indices: list[int] = field(default_factory=list)

    def __post_init__(self):
        if self.verdict not in ("accept", "counter", "reject"):
            raise ValueError(f"잘못된 verdict: {self.verdict}")
        self.counter_indices = list(self.counter_indices)[:MAX_COUNTER_INDICES]

    def to_json(self) -> dict:
        return {
            "protocol": PROTOCOL,
            "method": "negotiation/respond",
            "sessionId": self.session_id,
            "agentId": self.agent_id,
            "round": self.round,
            "verdict": self.verdict,
            "counterIndices": self.counter_indices,
        }

    @staticmethod
    def from_json(d: dict) -> "Response":
        return Response(
            session_id=d["sessionId"], agent_id=d["agentId"],
            round=d["round"], verdict=d["verdict"],
            counter_indices=d.get("counterIndices", []),
        )
