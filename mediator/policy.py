"""승인 정책: 전원 승인 / 최소 승인 인원 / 최소 승인 비율, 그리고 필수 참석자.

이 모듈은 순수 함수만 둔다(DB·네트워크 없음). 승인 상태 머신(approval_flow)과 일정 변경
투표(change_flow)가 같은 규칙으로 판정하도록 한 곳에 모았다.

규칙
  - 기본값은 전원 승인이다. 정책이 없는 기존 세션도 전원 승인으로 동작한다.
  - '승인 인원'은 필수 참석자를 포함한 전체 승인 수다.
  - 필수 참석자는 정족수와 별개로 전원이 승인해야 한다.
  - 주최자는 항상 필수다. 이벤트가 주최자 캘린더에 만들어지므로, 주최자가 빠진
    회의는 만들 수 없다. (mode='all' 이 아니면 build_policy 가 자동으로 넣는다.)
  - 필수 참석자가 거절하거나, 남은 사람이 모두 승인해도 정족수를 채울 수 없으면
    그 슬롯은 즉시 실패로 판정한다 → 호출자는 다음 후보로 넘어간다.

'가능 여부'(캘린더 충돌)는 이 정책과 별개다. 지금은 참가자 전원이 가능한 슬롯만
제안한다 — 정족수는 사람의 '동의'에만 적용된다.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

MODES = ("all", "min_count", "min_ratio")


class PolicyError(ValueError):
    """사용자에게 그대로 보여줄 수 있는 문장으로 사유를 담는다."""


@dataclass(frozen=True)
class ApprovalPolicy:
    mode: str = "all"
    min_count: int | None = None
    min_ratio: float | None = None
    required: tuple[str, ...] = ()          # agent_id 들

    def needed(self, total: int) -> int:
        """확정에 필요한 승인 수 (필수 참석자 포함)."""
        if self.mode == "all":
            return total
        if self.mode == "min_count":
            return int(self.min_count or total)
        if self.mode == "min_ratio":
            return max(1, math.ceil(float(self.min_ratio or 1.0) * total - 1e-9))
        raise PolicyError(f"알 수 없는 승인 방식: {self.mode}")

    def to_json(self) -> str:
        if self.mode == "all":
            return '{"mode":"all"}'        # 스키마의 DEFAULT 와 같은 문자열
        return json.dumps({"mode": self.mode, "min_count": self.min_count,
                           "min_ratio": self.min_ratio}, sort_keys=True,
                          separators=(",", ":"))

    @staticmethod
    def from_row(row) -> "ApprovalPolicy":
        """sessions 행에서 정책을 읽는다. 컬럼이 없거나 비어 있으면 전원 승인."""
        try:
            raw = row["approval_policy_json"]
            required = json.loads(row["required_agents_json"] or "[]")
        except (KeyError, IndexError, TypeError):
            return ALL_APPROVE
        try:
            d = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return ALL_APPROVE
        mode = d.get("mode", "all")
        if mode not in MODES or mode == "all":
            return ALL_APPROVE                # 알 수 없는 값은 가장 엄격한 쪽으로
        return ApprovalPolicy(mode, d.get("min_count"), d.get("min_ratio"),
                              tuple(required))


ALL_APPROVE = ApprovalPolicy("all")


def build_policy(mode: str = "all", *, min_count: int | None = None,
                 min_ratio: float | None = None, required=(),
                 participant_ids, organizer_id: str | None = None) -> ApprovalPolicy:
    """입력을 검증해 정책을 만든다. 잘못되면 PolicyError(사용자용 문장)."""
    pids = list(participant_ids)
    total = len(pids)
    if mode not in MODES:
        raise PolicyError(f"승인 방식은 {', '.join(MODES)} 중 하나여야 합니다")
    if total < 1:
        raise PolicyError("참가자가 없습니다")
    if mode == "all":
        return ALL_APPROVE

    req = []
    for a in required:
        if a not in pids:
            raise PolicyError("필수 참석자가 참가자 목록에 없습니다")
        if a not in req:
            req.append(a)
    if organizer_id and organizer_id in pids and organizer_id not in req:
        req.insert(0, organizer_id)

    if mode == "min_count":
        if not isinstance(min_count, int) or isinstance(min_count, bool):
            raise PolicyError("최소 승인 인원은 정수여야 합니다")
        if not 1 <= min_count <= total:
            raise PolicyError(f"최소 승인 인원은 1~{total}명이어야 합니다")
        pol = ApprovalPolicy("min_count", min_count=min_count, required=tuple(req))
    else:
        try:
            ratio = float(min_ratio)
        except (TypeError, ValueError):
            raise PolicyError("최소 승인 비율은 숫자여야 합니다") from None
        if not (0.0 < ratio <= 1.0):
            raise PolicyError("최소 승인 비율은 0보다 크고 1 이하여야 합니다")
        pol = ApprovalPolicy("min_ratio", min_ratio=ratio, required=tuple(req))

    needed = pol.needed(total)
    if needed < len(req):
        raise PolicyError(
            f"필수 참석자 {len(req)}명보다 적은 승인 인원({needed}명)으로는 "
            "확정 기준을 정할 수 없습니다")
    return pol


def evaluate(policy: ApprovalPolicy, participant_ids, approved, rejected) -> str:
    """현재 응답으로 이 제안이 어떻게 되는지 판정한다.

    Returns:
        'approved' 확정 기준 충족
        'failed'   필수 참석자가 거절했거나, 남은 사람이 모두 승인해도 정족수 불가
        'pending'  아직 결정할 수 없음
    """
    pids = list(participant_ids)
    total = len(pids)
    approved, rejected = set(approved), set(rejected)
    if policy.mode == "all":
        required, needed = set(pids), total
    else:
        required, needed = set(policy.required), policy.needed(total)

    if rejected & required:
        return "failed"
    if required <= approved and len(approved) >= needed:
        return "approved"
    if total - len(rejected) < needed:
        return "failed"
    return "pending"


# ---- 사용자에게 보여줄 문장 ----------------------------------------------------------

def describe(policy: ApprovalPolicy, total: int, names: dict[str, str] | None = None
             ) -> str:
    """승인 기준 설명. 필수 참석자 이름은 정책의 일부라 모든 참가자에게 공개된다.
    누가 거절했는지는 이 문장에 절대 들어가지 않는다."""
    if policy.mode == "all":
        return "참가자 전원이 동의해야 일정이 확정됩니다."
    names = names or {}
    req = [names.get(a, "필수 참석자") for a in policy.required]
    needed = policy.needed(total)
    who = ", ".join(req) if req else "없음"
    return (f"필수 참석자({who}) 전원과 총 {needed}명 이상(필수 참석자 포함)이 "
            f"동의하면 확정됩니다. 선택 참석자가 거절해도 이 기준을 채우면 확정될 수 있습니다.")


def role_text(policy: ApprovalPolicy, agent_id: str) -> str:
    if policy.mode == "all":
        return "이번 회의는 전원 동의가 필요합니다."
    if agent_id in policy.required:
        return "귀하는 필수 참석자입니다. 귀하가 거절하면 이 시간은 확정되지 않습니다."
    return "귀하는 선택 참석자입니다."
