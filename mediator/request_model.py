"""자연어 회의 요청을 담는 구조화 모델 (MeetingRequest 와 Constraint).

파서(규칙 기반 / LLM)의 출력은 신뢰하지 않는다. 어떤 파서든 결과는 이 모델로 옮겨지고,
constraints.py 의 결정론적 검증을 통과해야만 협상에 쓰인다.

Constraint 는 최소한 다음을 가진다.
    type         조건 종류 (CONSTRAINT_TYPES)
    value        조건 값 (종류별 스키마, 검증기가 확인)
    strength     hard: 어기면 후보에서 제외 / soft: 비용(선호 점수)에 반영
    priority     1(낮음)~5(높음). soft 페널티의 크기를 정한다
    source_text  원문 중 이 조건의 근거가 된 부분
    supported    현재 시스템이 이 조건을 실제로 처리할 수 있는가
    explanation  시스템이 이해한 의미 (사용자에게 그대로 보여준다)
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

STRENGTHS = ("hard", "soft")

# 조건 종류. 새 종류를 추가하려면 constraints.py 에 resolver 를 등록한다.
CONSTRAINT_TYPES = (
    "time_window",            # 이 시간대에만 (hard 필수 시간대)
    "excluded_time",          # 이 시간대는 제외/회피
    "preferred_time",         # 이 시간대를 선호
    "earliest_first",         # 가능한 한 빠른 시간
    "required_participant",   # 필수 참석자
    "quorum",                 # 최소 승인 인원/비율
    "buffer_time",            # 회의 전후 여유 / 이동 시간 / 연속 회의 회피
    "meeting_mode",           # 온라인 / 대면 / 혼합
    "location",               # 장소·위치
    "recurrence",             # 반복 일정
    "accessibility",          # 접근성
    "external_context",       # 날씨·교통·영업시간 등 외부 정보
    "other",                  # 해석하지 못한 조건 (항상 unsupported)
)

# ok            정상적으로 해석되어 처리됨
# unsupported   현재 자동 처리 불가 → 사용자가 수동 확인으로 넘기거나 제거해야 한다
# removed       사용자가 제거함
CONSTRAINT_STATUSES = ("ok", "unsupported", "removed")

ISSUE_KINDS = (
    "conflict",             # 서로 모순되는 조건 (해결 전에는 시작할 수 없다)
    "unknown_participant",  # 참가자 목록에 없는 이름 (해결 전에는 시작할 수 없다)
    "invalid",              # 값이 검증을 통과하지 못함 (해결 전에는 시작할 수 없다)
    "ignored",              # 시스템 변경/명령으로 보여 무시한 문장 (안내)
    "warning",              # 안내
)
BLOCKING_ISSUES = frozenset({"conflict", "unknown_participant", "invalid"})


@dataclass
class Constraint:
    id: str
    type: str
    value: dict = field(default_factory=dict)
    strength: str = "soft"
    priority: int = 3
    source_text: str = ""
    supported: bool = True
    explanation: str = ""
    status: str = "ok"
    acknowledged: bool = False      # unsupported 를 사용자가 '수동 확인'으로 받아들임

    def to_dict(self) -> dict:
        return {"id": self.id, "type": self.type, "value": self.value,
                "strength": self.strength, "priority": self.priority,
                "source_text": self.source_text, "supported": self.supported,
                "explanation": self.explanation, "status": self.status,
                "acknowledged": self.acknowledged}

    @staticmethod
    def from_dict(d: dict) -> "Constraint":
        return Constraint(
            id=str(d["id"]), type=str(d["type"]), value=dict(d.get("value") or {}),
            strength=d.get("strength", "soft"), priority=int(d.get("priority", 3)),
            source_text=str(d.get("source_text", "")),
            supported=bool(d.get("supported", True)),
            explanation=str(d.get("explanation", "")),
            status=d.get("status", "ok"), acknowledged=bool(d.get("acknowledged", False)))

    @property
    def active(self) -> bool:
        return self.status != "removed"


@dataclass
class Question:
    """모호한 조건에 대해 사용자에게 묻는 확인 질문. 답하기 전에는 시작할 수 없다."""
    id: str
    text: str
    options: list[dict]                 # [{"value": ..., "label": "..."}]
    constraint_id: str | None = None
    answer: object | None = None
    kind: str = ""                      # 답을 어떻게 반영할지 (nl_parse.apply_answer)
    data: dict = field(default_factory=dict)   # 종류별 부가 정보 (예: 오전/오후 해석 대상 필드)

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "options": self.options,
                "constraint_id": self.constraint_id, "answer": self.answer,
                "kind": self.kind, "data": self.data}

    @staticmethod
    def from_dict(d: dict) -> "Question":
        return Question(id=d["id"], text=d["text"], options=list(d.get("options", [])),
                        constraint_id=d.get("constraint_id"), answer=d.get("answer"),
                        kind=d.get("kind", ""), data=dict(d.get("data") or {}))


@dataclass
class Issue:
    kind: str
    message: str
    constraint_ids: list[str] = field(default_factory=list)
    origin: str = "eval"           # parse: 파서가 남긴 안내 / eval: 검증기가 매번 다시 계산

    def to_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message,
                "constraint_ids": self.constraint_ids, "origin": self.origin}

    @staticmethod
    def from_dict(d: dict) -> "Issue":
        return Issue(kind=d["kind"], message=d["message"],
                     constraint_ids=list(d.get("constraint_ids", [])),
                     origin=d.get("origin", "eval"))

    @property
    def blocking(self) -> bool:
        return self.kind in BLOCKING_ISSUES


@dataclass
class MeetingRequest:
    title: str = "FairMeet 협의 일정"
    purpose: str = ""
    duration_min: int = 60
    date_start: dt.date | None = None
    date_end: dt.date | None = None          # 포함(inclusive)
    timezone: str = "Asia/Seoul"
    mode: str = "work"                       # 기존 세션 모드 (work | social)
    priority: str = "normal"                 # low | normal | high
    required: list[str] = field(default_factory=list)   # 필수 참석자 이름
    approval: dict = field(default_factory=lambda: {"mode": "all"})
    constraints: list[Constraint] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # 사용자에게 보여줄 해석 요약
    source_text: str = ""
    # 참가자: None 이면 정해지지 않은 것(등록된 전체). 사용자가 확정한 뒤에는 세션에 스냅샷으로 저장된다.
    participant_ids: list[str] | None = None
    participant_source: str = "default"                 # default(기본값) | text(문장에서 추출) | user(직접 선택)
    # 사용자가 아직 확인하지 않은 '기본값으로 채운 항목' (duration, period, participants). 비워야 시작할 수 있다.
    defaults: list[str] = field(default_factory=list)

    # ---- 조회 ----
    def get(self, cid: str) -> Constraint | None:
        return next((c for c in self.constraints if c.id == cid), None)

    def active_constraints(self) -> list[Constraint]:
        return [c for c in self.constraints if c.active]

    def next_constraint_id(self) -> str:
        used = {c.id for c in self.constraints}
        n = 1
        while f"c{n}" in used:
            n += 1
        return f"c{n}"

    @property
    def days(self) -> int:
        if not self.date_start or not self.date_end:
            return 7
        return (self.date_end - self.date_start).days + 1

    # ---- 직렬화 ----
    def to_dict(self) -> dict:
        return {
            "title": self.title, "purpose": self.purpose,
            "duration_min": self.duration_min,
            "date_start": self.date_start.isoformat() if self.date_start else None,
            "date_end": self.date_end.isoformat() if self.date_end else None,
            "timezone": self.timezone, "mode": self.mode, "priority": self.priority,
            "required": list(self.required), "approval": dict(self.approval),
            "constraints": [c.to_dict() for c in self.constraints],
            "questions": [q.to_dict() for q in self.questions],
            "issues": [i.to_dict() for i in self.issues],
            "notes": list(self.notes), "source_text": self.source_text,
            "participant_ids": list(self.participant_ids) if self.participant_ids is not None else None,
            "participant_source": self.participant_source, "defaults": list(self.defaults),
        }

    @staticmethod
    def from_dict(d: dict) -> "MeetingRequest":
        def _d(x):
            return dt.date.fromisoformat(x) if x else None
        return MeetingRequest(
            title=d.get("title", "FairMeet 협의 일정"), purpose=d.get("purpose", ""),
            duration_min=int(d.get("duration_min", 60)),
            date_start=_d(d.get("date_start")), date_end=_d(d.get("date_end")),
            timezone=d.get("timezone", "Asia/Seoul"), mode=d.get("mode", "work"),
            priority=d.get("priority", "normal"), required=list(d.get("required", [])),
            approval=dict(d.get("approval") or {"mode": "all"}),
            constraints=[Constraint.from_dict(c) for c in d.get("constraints", [])],
            questions=[Question.from_dict(q) for q in d.get("questions", [])],
            issues=[Issue.from_dict(i) for i in d.get("issues", [])],
            notes=list(d.get("notes", [])), source_text=d.get("source_text", ""),
            participant_ids=(list(d["participant_ids"]) if d.get("participant_ids") is not None else None),
            participant_source=d.get("participant_source", "default"),
            defaults=list(d.get("defaults", [])))
