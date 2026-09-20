"""비용 함수. 개인정보에 접근하는 유일한 모듈이며, 결과로 나가는 것은
0~100 사이의 숫자와 거부권 플래그뿐이다.

시간별 비용은 아래 요소로만 계산한다. 아침형/저녁형 같은 취향 점수와 JSON 에 적힌 보호 시간·하루 허용량은 없다.
  - 고정 일정과 겹침                    → 거부권 (후보에서 제외)
  - 유동 일정과 겹침                    → 후보로 남기되 불편 비용 (FLEX_COST)
  - 회의 전후 여유 조건 (hard/soft)     → 고정 일정과 붙으면 거부권 또는 비용 (buffer)
  - 같은 날의 일정 밀도                  → 바쁜 시간(분)에 비례하는 비용
  - 일정과 떨어져 하루를 쪼개는 회의       → 고립 비용
  (과거 양보 잔액과 회의 초안의 hard/soft 시간 조건은 중재자가 별도로 반영한다.)
  - 모드 무관 수면 보호(07시 이전, 23시 이후)만 시스템 안전선으로 남긴다.

일정이 '고정'인지 '유동'인지는 사용자가 Google Calendar 색상으로 정한다 (agent/calendar_settings.py).
분류 설정이 없으면 모든 일정을 고정으로 본다.
"""
from __future__ import annotations

import datetime as dt

SCALE_VERSION = "cost-rules-v1"
MAX_COST = 100.0

# 모드 무관 절대 금지 경계 (수면 보호)
ABSOLUTE_EARLIEST = 7
ABSOLUTE_LATEST = 23

FLEX_COST = 35.0            # 옮길 수 있는 일정을 밀어내는 비용
ISOLATION_COST = 10.0       # 앞뒤 90분에 일정이 하나도 없는 회의
DENSITY_MINUTES_PER_POINT = 20.0
DENSITY_MAX = 25.0


def _overlap(a0: dt.datetime, a1: dt.datetime,
             b0: dt.datetime, b1: dt.datetime) -> bool:
    return a0 < b1 and b0 < a1

BUFFER_SOFT_COST = 20.0
MAX_BUFFER_MIN = 120


def parse_buffer_options(options) -> dict | None:
    """중재자가 보낸 회의 전후 여유 옵션을 검증한다. 없으면 None, 잘못되면 ValueError.

    options = {"before_min": int, "after_min": int, "strength": "hard" | "soft"}
    """
    if not options:
        return None
    if not isinstance(options, dict):
        raise ValueError("옵션 형식이 올바르지 않습니다")
    out = {"strength": options.get("strength", "soft")}
    if out["strength"] not in ("hard", "soft"):
        raise ValueError("여유 시간 강도는 hard/soft 여야 합니다")
    for k in ("before_min", "after_min"):
        v = options.get(k, 0)
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= MAX_BUFFER_MIN:
            raise ValueError(f"{k} 는 0~{MAX_BUFFER_MIN} 사이의 정수여야 합니다")
        out[k] = v
    return out if (out["before_min"] or out["after_min"]) else None


def cost_vector(
    slots: list[tuple[dt.datetime, dt.datetime]],
    events: list[tuple[dt.datetime, dt.datetime, str]],
    buffer: dict | None = None,
) -> tuple[list[float | None], list[bool]]:
    """슬롯별 비용과 거부권을 계산한다.

    events: 분류가 끝난 일정 [(시작, 끝, 'fixed' | 'flex')]. 무시할 일정은 미리 뺀다.
    buffer: 회의 전후 여유 (parse_buffer_options 의 결과). 회의 앞뒤 여유 구간 안에
    일정이 있으면 hard 는 거부권, soft 는 비용을 더한다. None 이면 여유 조건이 없다.

    Returns:
        (costs, veto_mask) — costs[i] 는 veto 면 None
    """
    costs: list[float | None] = []
    veto: list[bool] = []
    fixed = [(a, b) for a, b, k in events if k == "fixed"]
    flex = [(a, b) for a, b, k in events if k == "flex"]
    every = fixed + flex

    # 일별 점유 '분'. 구간 개수가 아니라 실제 시간으로 측정한다.
    busy_minutes_by_day: dict[dt.date, int] = {}
    for b0, b1 in every:
        minutes = int((b1 - b0).total_seconds() // 60)
        key = b0.astimezone(slots[0][0].tzinfo).date() if slots else b0.date()
        busy_minutes_by_day[key] = busy_minutes_by_day.get(key, 0) + minutes

    for (s, e) in slots:
        # 1) 고정 일정 — 제목을 모르는 채로 거부권만 행사한다
        blocked = any(_overlap(s, e, b0, b1) for b0, b1 in fixed)

        # 2) 회의 전후 여유: 앞뒤 여유 구간에 걸치는 일정이 있는가 (겹치는 일정은 위에서 이미 처리)
        buffer_hit = False
        if not blocked and buffer:
            margins = []
            if buffer["before_min"]:
                margins.append((s - dt.timedelta(minutes=buffer["before_min"]), s))
            if buffer["after_min"]:
                margins.append((e, e + dt.timedelta(minutes=buffer["after_min"])))
            # 옮길 수 있는 일정(유동)은 여유를 지켜야 할 대상이 아니다: 고정 일정만 본다
            buffer_hit = any(_overlap(m0, m1, b0, b1) for m0, m1 in margins for b0, b1 in fixed)
            if buffer_hit and buffer["strength"] == "hard":
                blocked = True

        # 3) 모드 무관 수면 보호
        if not blocked and (e.hour >= ABSOLUTE_LATEST or e.date() != s.date() or s.hour < ABSOLUTE_EARLIEST):
            blocked = True

        if blocked:
            costs.append(None)
            veto.append(True)
            continue

        c = 0.0
        if any(_overlap(s, e, f0, f1) for f0, f1 in flex):      # 옮길 수 있는 일정을 밀어냄
            c += FLEX_COST
        if buffer_hit:                                          # soft 여유 조건을 어기는 시간
            c += BUFFER_SOFT_COST
        # 그날이 얼마나 차 있는지 (분에 비례, 개인 허용량 같은 기준선은 두지 않는다)
        c += min(DENSITY_MAX, busy_minutes_by_day.get(s.date(), 0) / DENSITY_MINUTES_PER_POINT)
        # 앞뒤 90분이 비어 있으면 하루를 쪼개는 고립 회의
        near = any(_overlap(s - dt.timedelta(minutes=90), e + dt.timedelta(minutes=90), b0, b1)
                   for b0, b1 in every)
        if not near:
            c += ISOLATION_COST

        costs.append(round(min(MAX_COST, c), 2))
        veto.append(False)

    return costs, veto


def feasible_count(costs: list[float | None]) -> int:
    return sum(1 for c in costs if c is not None)