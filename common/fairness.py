"""공정성 solver. 외부 의존성 없음 — stdlib만 사용하므로 단독 테스트 가능.

핵심 용어:
    concession_balance (양보 잔액)
        양수 = 과거에 더 많이 희생했으므로 시스템이 배려해야 하는 상태
        음수 = 과거에 더 유리한 결과를 받았으므로 이번엔 양보할 차례
        0    = 균형

    'debt'(빚)이라는 이름을 쓰지 않는 이유: "양수 = 갚아야 함"으로
    읽혀 의미가 정반대로 뒤집히기 때문이다.
"""
from __future__ import annotations

from dataclasses import dataclass

DECAY_HALF_LIFE_ROUNDS = 6
BALANCE_CLAMP = 40.0
DEFAULT_LAMBDA = 1.0


@dataclass(frozen=True)
class SolveResult:
    slot_index: int
    regrets: dict[str, float]   # agent_id -> regret. 커밋 성공 후 잔액 갱신에 사용
    max_adjusted: float
    sum_regret: float
    penalty: float = 0.0        # 회의 조건(soft)의 메타 페널티. regrets/잔액에는 섞지 않는다


class NoFeasibleAgent(ValueError):
    """한 에이전트가 모든 슬롯을 거부해서 협상 자체가 성립하지 않음."""


def build_regret_matrix(
    costs: dict[str, list[float | None]],
) -> tuple[dict[str, list[float | None]], dict[str, float]]:
    """최초 비용 행렬에서 regret을 한 번만 계산한다.

    None = 거부권(veto). 반환된 regret 행렬은 협상 내내 변경하지 않으며,
    라운드마다 바뀌는 것은 candidate_mask 뿐이다. 이것이 매 라운드
    기준선이 흔들리는 문제(P0-3)를 구조적으로 막는다.

    regret을 '자기 최솟값 대비 상대값'으로 정의하기 때문에, 비용을
    일괄로 부풀리는 전략적 행동은 자동으로 무효화된다.

    Returns:
        (regret 행렬, agent별 min_feasible_cost)
    """
    regret: dict[str, list[float | None]] = {}
    baselines: dict[str, float] = {}

    for agent, vec in costs.items():
        feasible = [c for c in vec if c is not None]
        if not feasible:
            raise NoFeasibleAgent(f"에이전트 {agent} 에게 가능한 슬롯이 하나도 없습니다")
        base = min(feasible)
        baselines[agent] = base
        regret[agent] = [None if c is None else float(c) - base for c in vec]

    return regret, baselines


def solve(
    regret: dict[str, list[float | None]],
    balance: dict[str, float],
    candidate_mask: set[int],
    lam: float = DEFAULT_LAMBDA,
    slot_penalty: dict[int, float] | None = None,
) -> SolveResult | None:
    """사전식 최소화: (max_i adjusted_i + penalty_s, sum_i regret_i + n * penalty_s)

    1순위로 '가장 불리한 사람의 보정 후 불이익'을 최소화하고,
    동점이면 전체 불이익 합이 작은 쪽을 고른다.

    합계 최소화가 아니라 최댓값 최소화를 쓰는 이유: 합계는 한 명이
    크게 희생하고 나머지가 편한 해를 선택하기 때문이다.

    slot_penalty: 회의 단위의 soft 조건("가능하면 오후")이 슬롯에 매기는 페널티.
    모든 참가자에게 똑같이 얹히는 값이라 개인별 regret 과 양보 잔액에는 섞이지 않고
    슬롯 선택에만 영향을 준다. 주지 않으면(기본) 기존 동작과 완전히 같다.

    Args:
        regret: build_regret_matrix 가 만든 고정 행렬
        balance: agent_id -> concession_balance (양수 = 배려 대상)
        candidate_mask: 이번 라운드에 검토할 슬롯 인덱스 집합
        lam: 잔액 가중치
        slot_penalty: 슬롯 인덱스 -> 페널티 (없으면 0)

    Returns:
        SolveResult, 또는 가능한 슬롯이 없으면 None
    """
    agents = sorted(regret)
    if not agents:
        return None
    pen = slot_penalty or {}

    best: SolveResult | None = None
    best_key: tuple[float, float] | None = None

    for s in sorted(candidate_mask):
        row = {a: regret[a][s] for a in agents}
        if any(v is None for v in row.values()):
            continue                       # 거부권 → 절대 선택 불가

        p = float(pen.get(s, 0.0))
        adjusted = {a: row[a] + lam * balance.get(a, 0.0) for a in agents}
        key = (max(adjusted.values()) + p, sum(row.values()) + p * len(agents))

        if best_key is None or key < best_key:
            best_key = key
            best = SolveResult(
                slot_index=s,
                regrets={a: float(row[a]) for a in agents},
                max_adjusted=max(adjusted.values()),
                sum_regret=sum(row.values()),
                penalty=p,
            )

    return best


def update_balance(
    balance: dict[str, float],
    regrets: dict[str, float],
    half_life: int = DECAY_HALF_LIFE_ROUNDS,
    clamp: float = BALANCE_CLAMP,
) -> dict[str, float]:
    """확정된 슬롯의 regret으로 잔액을 갱신한다.

    반드시 인간 승인 + Calendar commit 성공 이후에만 호출할 것.
    평균 편차 기반이므로 decay를 제외하면 잔액 합은 항상 0으로 유지된다.
    """
    if not regrets:
        return dict(balance)

    decay = 0.5 ** (1.0 / half_life)
    mean_regret = sum(regrets.values()) / len(regrets)

    out = dict(balance)
    for agent, r in regrets.items():
        prev = out.get(agent, 0.0)
        nxt = prev * decay + (r - mean_regret)
        out[agent] = max(-clamp, min(clamp, nxt))
    return out


# ---- 베이스라인 전략 (평가 실험 전용) --------------------------------------

def solve_first_fit(regret, balance, candidate_mask, lam=0.0) -> SolveResult | None:
    """가장 이른 가능 슬롯. '빠른 사람이 먼저 잡는' 현실의 기본값."""
    agents = sorted(regret)
    for s in sorted(candidate_mask):
        row = {a: regret[a][s] for a in agents}
        if any(v is None for v in row.values()):
            continue
        return SolveResult(s, {a: float(row[a]) for a in agents},
                           max(row.values()), sum(row.values()))
    return None


def solve_sum_min(regret, balance, candidate_mask, lam=0.0) -> SolveResult | None:
    """총합 최소화. 효율은 좋지만 한 명이 크게 희생할 수 있다."""
    agents = sorted(regret)
    best, best_key = None, None
    for s in sorted(candidate_mask):
        row = {a: regret[a][s] for a in agents}
        if any(v is None for v in row.values()):
            continue
        key = (sum(row.values()), max(row.values()))
        if best_key is None or key < best_key:
            best_key = key
            best = SolveResult(s, {a: float(row[a]) for a in agents},
                               max(row.values()), sum(row.values()))
    return best


def solve_minimax(regret, balance, candidate_mask, lam=0.0) -> SolveResult | None:
    """잔액 보정이 없는 순수 minimax. FairMeet에서 잔액만 뺀 버전.

    이 전략과 solve() 의 차이가 곧 concession_balance 의 기여분이다.
    """
    return solve(regret, {a: 0.0 for a in regret}, candidate_mask, lam=0.0)


def gini(values: list[float]) -> float:
    """누적 부담의 불평등 지수. 0 = 완전 평등."""
    xs = sorted(values)
    n = len(xs)
    if n == 0:
        return 0.0
    shift = min(0.0, xs[0])
    xs = [x - shift for x in xs]          # 음수 방지
    total = sum(xs)
    if total == 0:
        return 0.0
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    return (2.0 * cum) / (n * total) - (n + 1.0) / n
