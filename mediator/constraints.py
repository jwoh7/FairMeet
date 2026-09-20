"""회의 조건의 결정론적 검증과 적용.

파서(규칙/LLM)가 만든 Constraint 는 여기서 항상 다시 검증된다. 이 모듈은 LLM 을 부르지
않고, 자연어를 실행하지 않으며, 외부 네트워크에 접근하지 않는다.

효과는 세 가지뿐이다.
  hard 조건   슬롯을 후보에서 제외한다 (Effects.excluded)
  soft 조건   슬롯에 페널티를 매긴다 (Effects.penalty). solve() 의 슬롯 선택에만 쓰이고
              개인별 regret 과 양보 잔액에는 섞이지 않는다.
  정책/옵션   승인 정책(필수 참석자·정족수)과, 에이전트에게 넘기는 옵션(회의 전후 여유)

조건 종류마다 ConstraintResolver 가 있고 RESOLVERS 에 등록된다. 날씨·교통·영업시간처럼
외부 정보가 필요한 조건은 ExternalContextProvider 를 register_provider() 로 붙이면 되며,
붙어 있지 않으면 '현재 자동 확인 불가'로 표시된다 (임의로 처리하거나 무시하지 않는다).
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Protocol

from mediator import policy as policy_mod
from mediator.request_model import STRENGTHS, Constraint, Issue, MeetingRequest

WEEKDAY_KO = "월화수목금토일"

# soft 조건이 슬롯에 매기는 페널티(비용 단위). 개인 regret 과 같은 0~100 스케일이다.
PRIORITY_WEIGHT = {1: 4.0, 2: 8.0, 3: 14.0, 4: 22.0, 5: 32.0}
PENALTY_CAP = 60.0
HIGH_PRIORITY_SCALE = 1.25
MIN_LEAD_MIN = 60                       # 지금부터 이 시간 이내의 슬롯은 제안하지 않는다
MAX_BUFFER_MIN = 120

_HHMM = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$|^24:00$")


# ---- 컨텍스트 / 효과 -------------------------------------------------------------------

@dataclass
class ResolveContext:
    slots: list                                   # [(start, end), ...] 협상에 쓸 슬롯 그리드
    participants: list[dict]                      # [{"agent_id", "name", "is_organizer"}]
    now: dt.datetime
    capabilities: dict | None = None              # agent_id -> set(str). None = 확인하지 못함
    providers: dict | None = None                 # kind -> ExternalContextProvider
    priority_scale: float = 1.0
    lead_minutes: int = MIN_LEAD_MIN

    def name_map(self) -> dict[str, dict]:
        return {p["name"]: p for p in self.participants}


@dataclass
class Effects:
    n_slots: int
    by_constraint: dict = field(default_factory=dict)     # cid -> 제외한 슬롯 집합
    past: set = field(default_factory=set)
    penalty: dict = field(default_factory=dict)           # 슬롯 -> 페널티
    required_names: list = field(default_factory=list)
    quorum: list = field(default_factory=list)            # quorum Constraint 들
    agent_options: dict = field(default_factory=dict)     # 참가자 이름 -> 옵션
    manual_checks: list = field(default_factory=list)

    def exclude(self, cid: str, idx: int) -> None:
        self.by_constraint.setdefault(cid, set()).add(idx)

    def add_penalty(self, idx: int, value: float) -> None:
        self.penalty[idx] = min(PENALTY_CAP, self.penalty.get(idx, 0.0) + value)

    @property
    def excluded(self) -> set:
        out = set(self.past)
        for s in self.by_constraint.values():
            out |= s
        return out


@dataclass
class Evaluation:
    excluded: set
    penalty: dict
    remaining: int
    policy: policy_mod.ApprovalPolicy
    required_names: list
    agent_options: dict                 # agent_id -> 옵션
    manual_checks: list
    issues: list
    excluded_by: dict                   # cid -> 제외한 슬롯 수

    def slot_effects(self) -> dict:
        """세션에 저장할 형태. 협상·재제안이 이 값을 그대로 읽는다."""
        return {"excluded": sorted(self.excluded),
                "penalty": {str(k): round(v, 2) for k, v in sorted(self.penalty.items())
                            if v > 0},
                "agent_options": self.agent_options,
                "manual_checks": list(self.manual_checks)}


# ---- 외부 조건 공급자 (확장 지점) ---------------------------------------------------------

@dataclass
class ProviderResult:
    excluded: set = field(default_factory=set)
    penalty: dict = field(default_factory=dict)


class ExternalContextProvider(Protocol):
    """날씨·교통·장소 영업시간 같은 외부 정보를 슬롯에 반영하는 공급자.

    구현하려면 kind, available(), assess() 를 갖춘 객체를 register_provider() 로 등록한다.
    이 저장소는 공급자를 기본 제공하지 않는다.
    """
    kind: str

    def available(self) -> bool: ...
    def assess(self, condition: dict, slots: list) -> ProviderResult: ...


_PROVIDERS: dict[str, ExternalContextProvider] = {}


def register_provider(provider: ExternalContextProvider) -> None:
    _PROVIDERS[provider.kind] = provider


def unregister_provider(kind: str) -> None:
    _PROVIDERS.pop(kind, None)


def registered_providers() -> dict:
    return dict(_PROVIDERS)


# ---- 시간대 명세 -----------------------------------------------------------------------

_SPEC_KEYS = {"weekdays", "dates", "start_time", "end_time", "label"}


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def validate_time_spec(v) -> str | None:
    """시간대 명세를 검증한다. 문제가 있으면 사용자에게 보일 문장을 돌려준다."""
    if not isinstance(v, dict):
        return "시간 조건의 값이 올바른 형식이 아닙니다"
    unknown = set(v) - _SPEC_KEYS
    if unknown:
        return f"알 수 없는 시간 조건 필드: {', '.join(sorted(map(str, unknown)))}"
    wd = v.get("weekdays")
    if wd is not None:
        if (not isinstance(wd, list) or not wd or len(wd) > 7
                or any(not isinstance(x, int) or isinstance(x, bool) or not 0 <= x <= 6
                       for x in wd)):
            return "요일은 0(월)~6(일) 정수 목록이어야 합니다"
    dates = v.get("dates")
    if dates is not None:
        if not isinstance(dates, list) or not dates or len(dates) > 31:
            return "날짜 목록이 올바르지 않습니다"
        for d in dates:
            try:
                dt.date.fromisoformat(d)
            except (TypeError, ValueError):
                return f"날짜 형식이 올바르지 않습니다: {str(d)[:20]}"
    for k in ("start_time", "end_time"):
        if k in v and v[k] is not None and (not isinstance(v[k], str)
                                            or not _HHMM.match(v[k])):
            return f"{k} 는 HH:MM 형식이어야 합니다"
    t0, t1 = v.get("start_time"), v.get("end_time")
    if t0 and t1 and _mins(t0) >= _mins(t1):
        return "시작 시각은 종료 시각보다 빨라야 합니다"
    if not (wd or dates or t0 or t1):
        return "요일·날짜·시각 중 하나는 있어야 합니다"
    label = v.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > 30):
        return "라벨이 올바르지 않습니다"
    return None


def slot_matches(slot, spec: dict, how: str) -> bool:
    """how='within': 슬롯이 명세 안에 완전히 들어감 / 'overlap': 조금이라도 겹침."""
    s, e = slot
    if spec.get("dates") and s.date().isoformat() not in spec["dates"]:
        return False
    if spec.get("weekdays") and s.weekday() not in spec["weekdays"]:
        return False
    t0 = _mins(spec["start_time"]) if spec.get("start_time") else 0
    t1 = _mins(spec["end_time"]) if spec.get("end_time") else 24 * 60
    s_min = s.hour * 60 + s.minute
    e_min = s_min + int((e - s).total_seconds() // 60)
    if how == "within":
        return s_min >= t0 and e_min <= t1
    return s_min < t1 and e_min > t0


def _fmt_weekdays(wd: list[int]) -> str:
    wd = sorted(set(wd))
    if wd == [0, 1, 2, 3, 4]:
        return "평일"
    if wd == [5, 6]:
        return "주말"
    return ", ".join(f"{WEEKDAY_KO[d]}요일" for d in wd)


def _fmt_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.month}월 {d.day}일({WEEKDAY_KO[d.weekday()]})"


def describe_spec(v: dict) -> str:
    parts = []
    if v.get("dates"):
        parts.append(", ".join(_fmt_date(d) for d in v["dates"]))
    if v.get("weekdays"):
        parts.append(_fmt_weekdays(v["weekdays"]))
    t0, t1 = v.get("start_time"), v.get("end_time")
    label = v.get("label")
    if t0 or t1:
        rng = f"{t0 or '00:00'}~{t1 or '24:00'}"
        parts.append(f"{label}({rng})" if label else rng)
    elif label:
        parts.append(label)
    return " ".join(parts) or "지정한 시간"


def _weight(c: Constraint, ctx: ResolveContext) -> float:
    return PRIORITY_WEIGHT.get(c.priority, 14.0) * ctx.priority_scale


# ---- Resolver 들 -----------------------------------------------------------------------

class ConstraintResolver:
    """조건 종류 하나를 처리한다. 새 종류는 서브클래스를 RESOLVERS 에 등록해 추가한다."""

    type = ""

    def validate(self, c: Constraint, ctx: ResolveContext):
        """문제가 있으면 (issue_kind, 문장), 없으면 None."""
        return None

    def support(self, c: Constraint, ctx: ResolveContext) -> tuple[bool, str]:
        """(지금 자동 처리 가능한가, 시스템이 이해한 의미)"""
        return True, ""

    def apply(self, c: Constraint, ctx: ResolveContext, fx: Effects) -> None:
        return None


class _TimeSpecResolver(ConstraintResolver):
    def validate(self, c, ctx):
        err = validate_time_spec(c.value)
        return ("invalid", err) if err else None


class TimeWindowConstraint(_TimeSpecResolver):
    type = "time_window"

    def support(self, c, ctx):
        return True, f"{describe_spec(c.value)} 안의 시간만 후보로 삼습니다 (그 밖은 제외)."

    def apply(self, c, ctx, fx):
        for i, slot in enumerate(ctx.slots):
            if not slot_matches(slot, c.value, "within"):
                fx.exclude(c.id, i)


class ExcludedTimeConstraint(_TimeSpecResolver):
    type = "excluded_time"

    def support(self, c, ctx):
        what = describe_spec(c.value)
        if c.strength == "hard":
            return True, f"{what}과(와) 겹치는 시간은 후보에서 제외합니다."
        return True, f"{what}은(는) 가능하면 피합니다 (겹치면 선호 점수가 낮아짐)."

    def apply(self, c, ctx, fx):
        w = _weight(c, ctx)
        for i, slot in enumerate(ctx.slots):
            if slot_matches(slot, c.value, "overlap"):
                if c.strength == "hard":
                    fx.exclude(c.id, i)
                else:
                    fx.add_penalty(i, w)


class PreferredTimeConstraint(_TimeSpecResolver):
    type = "preferred_time"

    def support(self, c, ctx):
        what = describe_spec(c.value)
        if c.strength == "hard":
            return True, f"{what} 안의 시간만 후보로 삼습니다 (그 밖은 제외)."
        return True, f"{what}을(를) 선호합니다 (그 밖의 시간은 선호 점수가 낮아짐)."

    def apply(self, c, ctx, fx):
        w = _weight(c, ctx)
        for i, slot in enumerate(ctx.slots):
            if not slot_matches(slot, c.value, "within"):
                if c.strength == "hard":
                    fx.exclude(c.id, i)
                else:
                    fx.add_penalty(i, w)


class EarliestFirstConstraint(ConstraintResolver):
    type = "earliest_first"

    def validate(self, c, ctx):
        if c.strength == "hard":
            return ("invalid", "'가장 빠른 시간'은 필수 조건으로 둘 수 없습니다 (선호로만 반영)")
        return None

    def support(self, c, ctx):
        return True, "가능한 한 빠른 시간을 선호합니다 (늦을수록 선호 점수가 낮아짐)."

    def apply(self, c, ctx, fx):
        if not ctx.slots:
            return
        starts = [s.timestamp() for s, _ in ctx.slots]
        lo, hi = min(starts), max(starts)
        span = hi - lo
        w = _weight(c, ctx)
        for i, (s, _) in enumerate(ctx.slots):
            frac = (s.timestamp() - lo) / span if span > 0 else 0.0
            fx.add_penalty(i, w * frac)


def _unknown_names(names, ctx) -> list[str]:
    known = ctx.name_map()
    return [n for n in names if n not in known]


class RequiredParticipantConstraint(ConstraintResolver):
    type = "required_participant"

    def validate(self, c, ctx):
        names = c.value.get("names")
        if (not isinstance(names, list) or not names
                or any(not isinstance(n, str) or not n.strip() for n in names)):
            return ("invalid", "필수 참석자 이름 목록이 올바르지 않습니다")
        bad = _unknown_names(names, ctx)
        if bad:
            return ("unknown_participant",
                    f"참가자 목록에 없는 이름입니다: {', '.join(bad)} "
                    f"(가능한 이름: {', '.join(ctx.name_map())})")
        return None

    def support(self, c, ctx):
        return True, (f"{', '.join(c.value['names'])} 님은 필수 참석자입니다. "
                      "이 사람이 거절하면 그 시간은 확정되지 않습니다.")

    def apply(self, c, ctx, fx):
        for n in c.value["names"]:
            if n not in fx.required_names:
                fx.required_names.append(n)


class QuorumConstraint(ConstraintResolver):
    type = "quorum"

    def validate(self, c, ctx):
        v = c.value
        kind = v.get("kind")
        if kind not in ("all", "min_count", "ratio", "majority", "required_only"):
            return ("invalid", "승인 기준 종류가 올바르지 않습니다")
        if kind == "min_count":
            n = v.get("count")
            if not isinstance(n, int) or isinstance(n, bool) or n < 1:
                return ("invalid", "최소 승인 인원은 1 이상의 정수여야 합니다")
            if v.get("scope", "total") not in ("total", "others"):
                return ("invalid", "인원 기준(scope)이 올바르지 않습니다")
        if kind == "ratio":
            r = v.get("ratio")
            if (not isinstance(r, (int, float)) or isinstance(r, bool)
                    or not 0 < r <= 1):
                return ("invalid", "최소 승인 비율은 0보다 크고 1 이하여야 합니다")
        return None

    def support(self, c, ctx):
        v = c.value
        total = len(ctx.participants)
        kind = v["kind"]
        if kind == "all":
            return True, "참가자 전원이 동의해야 확정됩니다."
        if kind == "required_only":
            return True, ("필수 참석자(주최자 포함)만 동의하면 확정됩니다. "
                          "선택 참석자의 응답은 필요하지 않습니다.")
        if kind == "majority":
            return True, f"과반({total // 2 + 1}명 이상, 필수 참석자 포함)이 동의하면 확정됩니다."
        if kind == "ratio":
            need = policy_mod.ApprovalPolicy("min_ratio", min_ratio=v["ratio"]).needed(total)
            return True, (f"전체의 {round(v['ratio'] * 100)}% 이상({need}명, 필수 참석자 포함)이 "
                          "동의하면 확정됩니다.")
        if v.get("scope") == "others":
            return True, (f"필수 참석자 외에 최소 {v['count']}명이 더 동의하면 확정됩니다 "
                          "(필수 참석자와 주최자는 항상 포함).")
        return True, f"필수 참석자를 포함해 최소 {v['count']}명이 동의하면 확정됩니다."

    def apply(self, c, ctx, fx):
        fx.quorum.append(c)


class BufferTimeConstraint(ConstraintResolver):
    """회의 전후 여유·이동 시간·연속 회의 회피. 캘린더는 에이전트만 보므로 에이전트가 처리한다."""

    type = "buffer_time"

    def validate(self, c, ctx):
        v = c.value
        for k in ("before_min", "after_min"):
            x = v.get(k, 0)
            if not isinstance(x, int) or isinstance(x, bool) or not 0 <= x <= MAX_BUFFER_MIN:
                return ("invalid", f"{k} 는 0~{MAX_BUFFER_MIN}분이어야 합니다")
        if not (v.get("before_min", 0) or v.get("after_min", 0)):
            return ("invalid", "여유 시간이 0분입니다")
        who = v.get("participants") or []
        if not isinstance(who, list) or any(not isinstance(n, str) for n in who):
            return ("invalid", "대상 참가자 목록이 올바르지 않습니다")
        bad = _unknown_names(who, ctx)
        if bad:
            return ("unknown_participant",
                    f"참가자 목록에 없는 이름입니다: {', '.join(bad)} "
                    f"(가능한 이름: {', '.join(ctx.name_map())})")
        return None

    def _scope(self, c, ctx) -> list[dict]:
        who = c.value.get("participants") or []
        return [p for p in ctx.participants if not who or p["name"] in who]

    def _meaning(self, c) -> str:
        v = c.value
        b, a = v.get("before_min", 0), v.get("after_min", 0)
        who = ", ".join(v.get("participants") or []) or "모든 참가자"
        span = (f"회의 전 {b}분·후 {a}분" if b and a
                else f"회의 전 {b}분" if b else f"회의 후 {a}분")
        if c.strength == "hard":
            return f"{who}의 캘린더에 {span} 안에 일정이 있으면 그 시간은 제외합니다."
        return f"{who}의 캘린더에 {span} 안에 일정이 있으면 선호 점수를 낮춥니다."

    def support(self, c, ctx):
        scope = self._scope(c, ctx)
        caps = ctx.capabilities
        if caps is None:
            return False, ("참가자 에이전트가 이 조건을 지원하는지 확인하지 못했습니다. "
                           "직접 확인해 주세요.")
        lacking = [p["name"] for p in scope if "buffer" not in caps.get(p["agent_id"], set())]
        if lacking:
            return False, (f"{', '.join(lacking)} 님의 에이전트가 회의 전후 여유 조건을 아직 "
                           "지원하지 않아 자동 확인할 수 없습니다 (에이전트 서버를 최신 "
                           "버전으로 다시 시작하면 지원됩니다). 직접 확인해 주세요.")
        return True, self._meaning(c)

    def apply(self, c, ctx, fx):
        v = c.value
        for p in self._scope(c, ctx):
            cur = fx.agent_options.setdefault(p["name"], {"before_min": 0, "after_min": 0,
                                                         "strength": "soft"})
            cur["before_min"] = max(cur["before_min"], v.get("before_min", 0))
            cur["after_min"] = max(cur["after_min"], v.get("after_min", 0))
            if c.strength == "hard":
                cur["strength"] = "hard"


class MeetingModeConstraint(ConstraintResolver):
    type = "meeting_mode"

    def validate(self, c, ctx):
        v = c.value
        modes = {"online", "in_person", "hybrid"}
        if v.get("mode") not in modes:
            return ("invalid", "회의 방식은 online / in_person / hybrid 중 하나여야 합니다")
        if v.get("preferred") is not None and v["preferred"] not in modes:
            return ("invalid", "선호 방식이 올바르지 않습니다")
        return None

    def support(self, c, ctx):
        v = c.value
        if v["mode"] == "online" and v.get("preferred") in (None, "online"):
            return True, "온라인 회의(Google Meet 링크 포함)로 등록됩니다."
        return False, ("대면·혼합 회의는 참가자별 대면 가능 여부와 장소 정보가 없어 자동으로 "
                       "확인할 수 없습니다. 직접 확인해 주세요.")


class _UnsupportedConstraint(ConstraintResolver):
    reason = "현재 자동으로 처리할 수 없는 조건입니다. 직접 확인해 주세요."

    def support(self, c, ctx):
        return False, self.reason


class LocationConstraint(_UnsupportedConstraint):
    type = "location"
    reason = ("장소·이동 조건은 위치 정보와 지도·교통 공급자가 없어 자동으로 확인할 수 "
              "없습니다. 직접 확인해 주세요.")


class RecurrenceConstraint(_UnsupportedConstraint):
    type = "recurrence"
    reason = ("반복 일정은 여러 주의 공통 가능 시간 탐색과 반복 이벤트 생성이 필요해 아직 "
              "지원하지 않습니다. 이번에는 한 번의 회의만 잡고, 반복은 직접 설정해 주세요.")


class AccessibilityConstraint(_UnsupportedConstraint):
    type = "accessibility"
    reason = ("접근성 조건(휠체어 접근 등)은 장소 정보가 없어 자동으로 확인할 수 없습니다. "
              "장소를 직접 확인해 주세요.")


class OtherConstraint(_UnsupportedConstraint):
    type = "other"
    reason = "이 문장은 조건으로 해석하지 못했습니다. 자동으로 반영되지 않습니다."


class ExternalContextConstraint(ConstraintResolver):
    """날씨·교통·영업시간. 공급자가 등록되어 있으면 슬롯에 반영하고, 없으면 자동 확인 불가."""

    type = "external_context"
    KINDS = ("weather", "traffic", "venue_hours")

    def validate(self, c, ctx):
        if c.value.get("kind") not in self.KINDS:
            return ("invalid", f"외부 조건 종류는 {', '.join(self.KINDS)} 중 하나여야 합니다")
        return None

    def _provider(self, c, ctx):
        providers = ctx.providers if ctx.providers is not None else registered_providers()
        p = providers.get(c.value["kind"])
        try:
            return p if p is not None and p.available() else None
        except Exception:                                        # noqa: BLE001
            return None

    def support(self, c, ctx):
        label = {"weather": "날씨", "traffic": "교통", "venue_hours": "장소 영업시간"}[
            c.value["kind"]]
        if self._provider(c, ctx) is None:
            return False, (f"{label} 조건은 연결된 정보 공급자가 없어 현재 자동 확인할 수 "
                           "없습니다. 직접 확인하거나 조건을 제거해 주세요.")
        return True, f"{label} 정보를 반영해 후보 시간을 거릅니다."

    def apply(self, c, ctx, fx):
        p = self._provider(c, ctx)
        if p is None:
            return
        res = p.assess(c.value, ctx.slots)
        w = _weight(c, ctx)
        for i in res.excluded:
            if c.strength == "hard":
                fx.exclude(c.id, i)
            else:
                fx.add_penalty(i, w)
        for i, v in res.penalty.items():
            fx.add_penalty(int(i), float(v))


RESOLVERS: dict[str, ConstraintResolver] = {r.type: r for r in (
    TimeWindowConstraint(), ExcludedTimeConstraint(), PreferredTimeConstraint(),
    EarliestFirstConstraint(), RequiredParticipantConstraint(), QuorumConstraint(),
    BufferTimeConstraint(), MeetingModeConstraint(), LocationConstraint(),
    RecurrenceConstraint(), AccessibilityConstraint(), ExternalContextConstraint(),
    OtherConstraint(),
)}


# ---- 평가 ------------------------------------------------------------------------------

def _label(c: Constraint) -> str:
    return f"'{c.source_text}'" if c.source_text else f"조건 {c.id}"


def _find_conflict(req_constraints, fx: Effects, n: int) -> tuple[list[str], str]:
    """후보가 0개일 때, 어떤 조건이 원인인지 가장 작은 묶음으로 찾는다."""
    by = fx.by_constraint
    cids = list(by)
    base = set(fx.past)
    if len(base) == n:
        return [], "탐색 기간의 시간이 모두 지났습니다 (지금부터 1시간 뒤부터 잡을 수 있습니다)."
    label = {c.id: _label(c) for c in req_constraints}
    for cid in cids:
        if len(base | by[cid]) == n:
            return [cid], f"{label[cid]} 조건만으로 모든 후보 시간이 사라집니다."
    for i, a in enumerate(cids):
        for b in cids[i + 1:]:
            if len(base | by[a] | by[b]) == n:
                return [a, b], (f"{label[a]} 와(과) {label[b]} 조건을 함께 만족하는 "
                                "시간이 없습니다.")
    return cids, "필수·제외 조건을 모두 만족하는 시간이 없습니다."


def evaluate_request(req: MeetingRequest, ctx: ResolveContext) -> Evaluation:
    """모든 조건을 다시 검증·해석하고 효과를 계산한다. 항상 결정론적이다.

    부작용: 각 Constraint 의 supported / status / explanation 을 갱신한다.
    """
    fx = Effects(n_slots=len(ctx.slots))
    issues: list[Issue] = []
    if req.priority == "high":
        ctx.priority_scale = HIGH_PRIORITY_SCALE

    for c in req.constraints:
        if not c.active:
            continue
        resolver = RESOLVERS.get(c.type)
        if (resolver is None or c.strength not in STRENGTHS
                or not isinstance(c.priority, int) or not 1 <= c.priority <= 5):
            c.supported, c.status = False, "unsupported"
            c.explanation = "조건 형식이 올바르지 않아 처리하지 않습니다."
            issues.append(Issue("invalid", f"{_label(c)}: 조건 종류·강도·우선순위가 올바르지 "
                                           "않습니다", [c.id]))
            continue
        problem = resolver.validate(c, ctx)
        if problem:
            kind, msg = problem
            c.supported, c.status, c.explanation = False, "unsupported", msg
            issues.append(Issue(kind, f"{_label(c)}: {msg}", [c.id]))
            continue
        ok, expl = resolver.support(c, ctx)
        c.supported, c.explanation = ok, expl
        c.status = "ok" if ok else "unsupported"
        if not ok:
            fx.manual_checks.append(f"{_label(c)}: {expl}")
            continue
        resolver.apply(c, ctx, fx)

    # 이미 지났거나 너무 임박한 슬롯은 어떤 조건이 있든 제안하지 않는다.
    cutoff = ctx.now + dt.timedelta(minutes=ctx.lead_minutes)
    for i, (s, _) in enumerate(ctx.slots):
        if s < cutoff:
            fx.past.add(i)

    # 후보가 하나도 없으면 원인이 된 조건을 짚어 충돌로 알린다.
    n = len(ctx.slots)
    remaining = n - len(fx.excluded)
    if n == 0:
        issues.append(Issue("conflict", "탐색 기간에 회의를 잡을 수 있는 시간대가 없습니다 "
                                        "(업무 모드는 평일 09~19시만 탐색합니다). 기간을 "
                                        "바꿔 주세요.", []))
    elif remaining <= 0:
        culprits, msg = _find_conflict(req.active_constraints(), fx, n)
        issues.append(Issue("conflict", msg, culprits))
    elif remaining <= 3:
        issues.append(Issue("warning", f"조건을 만족하는 후보 시간이 {remaining}개뿐입니다 "
                                       "(캘린더 확인 전 기준).", []))

    # 같은 시간대를 필수와 제외로 동시에 요청한 명백한 모순
    hard_win = [c for c in req.active_constraints()
                if c.supported and c.strength == "hard" and c.type in
                ("time_window", "preferred_time", "excluded_time")]
    for a in hard_win:
        for b in hard_win:
            if (a.type != "excluded_time" and b.type == "excluded_time"
                    and a.value == b.value and a.id != b.id):
                issues.append(Issue("conflict", f"{_label(a)} 와(과) {_label(b)} 은(는) 같은 "
                                                "시간대를 필수와 제외로 동시에 요청합니다.",
                                    [a.id, b.id]))

    policy = _build_policy(fx, ctx, issues)

    name_to_id = ctx.name_map()
    options = {}
    for name, opt in fx.agent_options.items():
        options[name_to_id[name]["agent_id"]] = {
            "before_min": opt["before_min"], "after_min": opt["after_min"],
            "strength": opt["strength"]}

    # 페널티는 제외된 슬롯에는 의미가 없으므로 남겨두지 않는다.
    excluded = fx.excluded
    penalty = {i: v for i, v in fx.penalty.items() if i not in excluded and v > 0}
    return Evaluation(
        excluded=excluded, penalty=penalty, remaining=max(0, remaining), policy=policy,
        required_names=list(fx.required_names), agent_options=options,
        manual_checks=fx.manual_checks, issues=issues,
        excluded_by={cid: len(s) for cid, s in fx.by_constraint.items()})


def _build_policy(fx: Effects, ctx: ResolveContext, issues: list) -> policy_mod.ApprovalPolicy:
    pids = [p["agent_id"] for p in ctx.participants]
    if not pids:
        return policy_mod.ALL_APPROVE
    by_name = ctx.name_map()
    organizer = next((p["agent_id"] for p in ctx.participants if p.get("is_organizer")), None)
    required_ids = [by_name[n]["agent_id"] for n in fx.required_names if n in by_name]
    total = len(pids)

    if len(fx.quorum) > 1:
        issues.append(Issue("conflict", "승인 기준이 여러 개 지정되어 있습니다. 하나만 남겨 주세요.",
                            [c.id for c in fx.quorum]))
        return policy_mod.ALL_APPROVE
    if not fx.quorum:
        return policy_mod.ALL_APPROVE          # 기본값: 전원 승인 (필수 참석자는 전원이라 무의미)

    q = fx.quorum[0]
    v = q.value
    kind = v["kind"]
    try:
        if kind == "all":
            return policy_mod.ALL_APPROVE
        if kind == "required_only":
            base = set(required_ids) | ({organizer} if organizer else set())
            return policy_mod.build_policy(
                "min_count", min_count=max(1, len(base)), required=required_ids,
                participant_ids=pids, organizer_id=organizer)
        if kind == "majority":
            return policy_mod.build_policy(
                "min_count", min_count=total // 2 + 1, required=required_ids,
                participant_ids=pids, organizer_id=organizer)
        if kind == "ratio":
            return policy_mod.build_policy(
                "min_ratio", min_ratio=float(v["ratio"]), required=required_ids,
                participant_ids=pids, organizer_id=organizer)
        count = v["count"]
        if v.get("scope") == "others":
            base = set(required_ids) | ({organizer} if organizer else set())
            count = len(base) + count
        return policy_mod.build_policy(
            "min_count", min_count=count, required=required_ids,
            participant_ids=pids, organizer_id=organizer)
    except policy_mod.PolicyError as exc:
        issues.append(Issue("conflict", f"{_label(q)}: {exc}",
                            [q.id] + [c.id for c in fx.quorum if c.id != q.id]))
        return policy_mod.ALL_APPROVE
