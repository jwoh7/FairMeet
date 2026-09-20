"""4개 전략 비교 실험 — 발표에서 차별점을 숫자로 증명하는 자료.

실행:
    python -m eval.compare
    python -m eval.compare --rounds 50 --seeds 5

정직하게 읽는 법은 README 와 출력 하단의 해설을 볼 것.
FairMeet 은 '모든 지표에서 최고'가 아니라 '분배가 가장 고르다'.
"""
from __future__ import annotations

import argparse
import random
import statistics
import sys

from common.fairness import (
    NoFeasibleAgent, build_regret_matrix, gini, solve, solve_first_fit,
    solve_minimax, solve_sum_min, update_balance,
)

STRATEGIES = {
    "선착순(first-fit)": solve_first_fit,
    "합계 최소화(sum-min)": solve_sum_min,
    "minimax (잔액 없음)": solve_minimax,
    "FairMeet (잔액 보정)": solve,
}

N_SLOTS = 40
AGENTS = ["A", "B", "C"]


def make_scenario(rng: random.Random, veto_rate: float = 0.25) -> dict:
    """A 는 구조적으로 제약이 많은 사용자.

    돌봄·통원·아르바이트를 병행하는 사람을 모델링한다. 이런 사람이
    반복해서 손해를 보는지가 이 실험의 핵심 질문이다.
    """
    costs = {}
    for a in AGENTS:
        rate = veto_rate * (1.8 if a == "A" else 1.0)
        bias = 25.0 if a == "A" else 0.0
        vec = []
        for _ in range(N_SLOTS):
            if rng.random() < rate:
                vec.append(None)
            else:
                vec.append(min(100.0, max(0.0, rng.uniform(0, 70) + bias)))
        if all(v is None for v in vec):
            vec[rng.randrange(N_SLOTS)] = 50.0
        costs[a] = vec
    return costs


def run(strategy, rounds: int = 30, seed: int = 7) -> dict:
    rng = random.Random(seed)
    balance = {a: 0.0 for a in AGENTS}
    cumulative = {a: 0.0 for a in AGENTS}
    failures = 0
    per_round_max = []

    for _ in range(rounds):
        costs = make_scenario(rng)
        try:
            regret, _ = build_regret_matrix(costs)
        except NoFeasibleAgent:
            failures += 1
            continue
        mask = set(range(N_SLOTS))
        res = strategy(regret, balance, mask)
        if res is None:
            failures += 1
            continue
        for a, r in res.regrets.items():
            cumulative[a] += r
        per_round_max.append(max(res.regrets.values()))
        balance = update_balance(balance, res.regrets)

    vals = list(cumulative.values())
    return {
        "cumulative": cumulative,
        "gini": gini(vals),
        "total": sum(vals),
        "max_individual": max(vals),
        "spread": max(vals) - min(vals),
        "stdev": statistics.pstdev(vals),
        "worst_round": max(per_round_max) if per_round_max else 0.0,
        "failures": failures,
    }


def no_common_slot_case() -> dict:
    """공통 가능 슬롯이 없는 사례 — 모든 전략이 None 을 반환해야 한다."""
    costs = {"A": [None, 5.0], "B": [5.0, None], "C": [5.0, 5.0]}
    regret, _ = build_regret_matrix(costs)
    return {name: fn(regret, {}, {0, 1}) for name, fn in STRATEGIES.items()}


def main() -> int:
    if sys.platform == "win32":
        # Windows 콘솔의 기본 코드페이지(예: CP949)는 ─ 등을 표현하지 못해
        # UnicodeEncodeError로 죽는다. 표준출력/에러를 UTF-8로 강제한다.
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (AttributeError, ValueError):
                pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=30)
    ap.add_argument("--seeds", type=int, default=1,
                    help="여러 시드의 평균을 낸다 (변동성 확인용)")
    args = ap.parse_args()

    print(f"시나리오: {len(AGENTS)}인, 슬롯 {N_SLOTS}개, {args.rounds}라운드 반복")
    print(f"          A 는 구조적으로 제약이 많은 사용자 "
          f"(거부권 1.8배, 비용 +25)\n")

    header = (f"{'전략':<24}{'A':>7}{'B':>7}{'C':>7}"
              f"{'Gini':>8}{'총합':>8}{'편차':>7}{'실패':>6}")
    print(header)
    print("─" * len(header))

    results = {}
    for name, fn in STRATEGIES.items():
        runs = [run(fn, args.rounds, seed=7 + i) for i in range(args.seeds)]
        agg = {
            "cumulative": {a: statistics.mean(r["cumulative"][a] for r in runs)
                           for a in AGENTS},
            "gini": statistics.mean(r["gini"] for r in runs),
            "total": statistics.mean(r["total"] for r in runs),
            "spread": statistics.mean(r["spread"] for r in runs),
            "max_individual": statistics.mean(r["max_individual"] for r in runs),
            "failures": sum(r["failures"] for r in runs),
        }
        results[name] = agg
        c = agg["cumulative"]
        print(f"{name:<24}{c['A']:>7.0f}{c['B']:>7.0f}{c['C']:>7.0f}"
              f"{agg['gini']:>8.3f}{agg['total']:>8.0f}"
              f"{agg['spread']:>7.0f}{agg['failures']:>6}")

    print("\n공통 가능 슬롯이 없는 경우:")
    for name, res in no_common_slot_case().items():
        print(f"  {name:<24} → {res}")
    print("  (전부 None 이어야 정상 — 실패 경로가 살아 있다는 뜻)")

    fm = results["FairMeet (잔액 보정)"]
    mm = results["minimax (잔액 없음)"]
    sm = results["합계 최소화(sum-min)"]
    ff = results["선착순(first-fit)"]

    print("\n" + "─" * len(header))
    print("해석 (발표 전에 반드시 읽을 것)\n")
    print(f"1. Gini: FairMeet {fm['gini']:.3f} vs minimax {mm['gini']:.3f}")
    print(f"   → 잔액 보정만 추가했을 뿐인데 불평등이 "
          f"{(1 - fm['gini'] / mm['gini']) * 100:.0f}% 감소했습니다.")
    print(f"   이것이 concession_balance 의 존재 이유를 증명하는 한 줄입니다.\n")
    print(f"2. 총 부담: FairMeet {fm['total']:.0f} vs sum-min {sm['total']:.0f}")
    print(f"   → sum-min 이 더 낮습니다. 당연합니다 — 총합이 아니라 분배를")
    print(f"   최적화하니까요. '모든 면에서 낫다'고 말하지 마세요.\n")
    print(f"3. 편차(최대-최소): FairMeet {fm['spread']:.0f} vs "
          f"sum-min {sm['spread']:.0f}")
    print(f"   → 세 사람의 누적 부담이 서로 얼마나 가까운지. "
          f"이것이 FairMeet 의 실제 강점입니다.\n")
    print(f"4. first-fit 은 Gini {ff['gini']:.3f} 로 낮아 보이지만 "
          f"총 부담이 {ff['total'] / fm['total']:.1f}배입니다.")
    print(f"   '공평하게 모두가 불행한' 해입니다. "
          f"Gini 만 보면 안 되는 이유입니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
