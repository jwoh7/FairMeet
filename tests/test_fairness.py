"""공정성 solver 테스트. 청사진 §8.3 의 수치 예시가 코드와 일치하는지 검증한다."""
from common.fairness import (
    NoFeasibleAgent, build_regret_matrix, gini, solve, solve_first_fit,
    solve_minimax, solve_sum_min, update_balance,
)


def test_regret_is_relative_to_own_minimum():
    costs = {"A": [40.0, 10.0, None], "B": [10.0, 35.0, 20.0]}
    regret, base = build_regret_matrix(costs)
    assert base == {"A": 10.0, "B": 10.0}
    assert regret["A"] == [30.0, 0.0, None]
    assert regret["B"] == [0.0, 25.0, 10.0]


def test_uniform_cost_inflation_has_no_effect():
    """일괄 부풀림 공격이 regret 정규화로 자동 무효화되는지.

    이것은 발표에서 주장해도 되는 실제 강점이다.
    """
    honest = {"A": [40.0, 10.0, 60.0], "B": [10.0, 35.0, 20.0]}
    inflated = {"A": [90.0, 60.0, 110.0], "B": [10.0, 35.0, 20.0]}
    assert build_regret_matrix(honest)[0] == build_regret_matrix(inflated)[0]


def test_balance_flips_the_decision():
    """§8.3 예시 1 — 잔액이 쌓이면 다음 라운드에 결과가 뒤집힌다."""
    costs = {"A": [40.0, 10.0, None], "B": [10.0, 35.0, 20.0]}
    regret, _ = build_regret_matrix(costs)
    mask = {0, 1, 2}

    r0 = solve(regret, {"A": 0.0, "B": 0.0}, mask)
    assert r0.slot_index == 1, "첫 라운드는 s2 (max regret 25 최소)"

    bal = update_balance({}, r0.regrets)
    assert bal["A"] < 0 < bal["B"], f"부호가 뒤집힘: {bal}"

    r1 = solve(regret, bal, mask)
    assert r1.slot_index == 0, "둘째 라운드는 s1 으로 뒤집혀야 함"


def test_positive_balance_means_consideration():
    """양수 잔액 = 배려 대상. 부호 의미가 문서와 일치하는지."""
    regret = {"A": [30.0, 5.0], "B": [5.0, 32.0], "C": [5.0, 5.0]}
    assert solve(regret, {}, {0, 1}).slot_index == 0        # 잔액 없으면 s1
    bal = {"A": 20.0, "B": 0.0, "C": -20.0}                 # A 를 배려해야 함
    assert solve(regret, bal, {0, 1}).slot_index == 1       # A 의 regret 낮은 s2


def test_veto_slot_is_never_selected():
    costs = {"A": [None, 90.0], "B": [0.0, 90.0]}
    regret, _ = build_regret_matrix(costs)
    assert solve(regret, {}, {0, 1}).slot_index == 1


def test_veto_wins_over_any_balance():
    """잔액이 아무리 커도 거부권을 이길 수 없다."""
    costs = {"A": [None, 100.0], "B": [0.0, 100.0]}
    regret, _ = build_regret_matrix(costs)
    res = solve(regret, {"A": 40.0, "B": -40.0}, {0, 1})
    assert res.slot_index == 1


def test_no_feasible_slot_returns_none():
    costs = {"A": [None, 5.0], "B": [5.0, None]}
    regret, _ = build_regret_matrix(costs)
    assert solve(regret, {}, {0, 1}) is None
    assert solve_sum_min(regret, {}, {0, 1}) is None
    assert solve_first_fit(regret, {}, {0, 1}) is None
    assert solve_minimax(regret, {}, {0, 1}) is None


def test_agent_with_no_feasible_slot_raises():
    costs = {"A": [None, None], "B": [5.0, 5.0]}
    try:
        build_regret_matrix(costs)
    except NoFeasibleAgent:
        return
    raise AssertionError("모든 슬롯을 거부한 에이전트가 통과됨")


def test_balance_is_zero_sum():
    bal = update_balance({}, {"A": 30.0, "B": 0.0, "C": 15.0})
    assert abs(sum(bal.values())) < 1e-9


def test_balance_is_clamped():
    bal = {}
    for _ in range(50):
        bal = update_balance(bal, {"A": 100.0, "B": 0.0})
    assert abs(bal["A"]) <= 40.0 + 1e-9


def test_balance_decays():
    """오래된 부담은 서서히 잊힌다."""
    bal = update_balance({}, {"A": 30.0, "B": 0.0})
    start = bal["A"]
    for _ in range(10):
        bal = update_balance(bal, {"A": 0.0, "B": 0.0})
    assert abs(bal["A"]) < abs(start) * 0.6


def test_minimax_differs_from_sum_min():
    """§8.3 예시 2 — 합계 최소화와 minimax 가 다른 답을 낼 수 있다."""
    costs = {"A": [30.0, 5.0, 12.0], "B": [5.0, 32.0, 12.0], "C": [5.0, 5.0, 12.0]}
    regret, _ = build_regret_matrix(costs)
    sm = solve_sum_min(regret, {}, {0, 1, 2})
    mm = solve_minimax(regret, {}, {0, 1, 2})
    assert sm.sum_regret <= mm.sum_regret       # 합계는 sum_min 이 우위
    assert mm.max_adjusted <= sm.max_adjusted   # 최댓값은 minimax 가 우위


def test_lexicographic_tiebreak_uses_sum():
    """max 가 같으면 sum 이 작은 쪽."""
    regret = {"A": [10.0, 10.0], "B": [10.0, 0.0]}
    assert solve(regret, {}, {0, 1}).slot_index == 1


def test_gini_bounds():
    assert abs(gini([10.0, 10.0, 10.0])) < 1e-9
    assert gini([0.0, 0.0, 30.0]) > 0.5
    assert gini([]) == 0.0
    assert gini([0.0, 0.0]) == 0.0


def test_solve_result_regrets_match_matrix():
    """SolveResult.regrets 가 잔액 갱신에 그대로 쓰이므로 정확해야 한다."""
    costs = {"A": [40.0, 10.0], "B": [10.0, 35.0]}
    regret, _ = build_regret_matrix(costs)
    res = solve(regret, {}, {0, 1})
    for a, r in res.regrets.items():
        assert r == regret[a][res.slot_index]
