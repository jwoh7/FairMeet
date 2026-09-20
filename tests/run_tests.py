"""pytest 없이도 돌아가는 테스트 러너.

해커톤 현장에서 pip install 이 막히는 경우가 있어서, pytest 가 있으면
pytest 를 쓰고 없으면 이 러너가 같은 테스트 함수를 실행한다.

    python -m tests.run_tests          # 전체
    python -m tests.run_tests fairness # 이름에 fairness 가 들어간 것만
"""
from __future__ import annotations

import importlib
import inspect
import sys
import traceback
from pathlib import Path

TEST_MODULES = [
    "tests.test_fairness",
    "tests.test_slots",
    "tests.test_cost",
    "tests.test_payload_guard",
    "tests.test_store",
    "tests.test_negotiation",
    "tests.test_commit",
    "tests.test_http_commit",
    "tests.test_no_event_read",
    "tests.test_migration",
    "tests.test_approval_tokens",
    "tests.test_approval_flow",
    "tests.test_notifier",
    "tests.test_approval_web",
    "tests.test_http_reproposal",
    "tests.test_google_calendar_errors",
    "tests.test_policy",
    "tests.test_quorum_flow",
    "tests.test_migration_v3",
    "tests.test_nl_constraints",
    "tests.test_nl_flow",
    "tests.test_constraints_solver",
    "tests.test_change_flow",
    "tests.test_change_e2e",
    "tests.test_http_agent_features",
    "tests.test_prep",
    "tests.test_guard",
    "tests.test_admin_auth",
    "tests.test_product_flow",
    "tests.test_material_flow",
    "tests.test_migration_v4",
    "tests.test_public_pages",
    "tests.test_nl_workshop_bug",
    "tests.test_draft_confirm_button",
]

G, R, Y, X, D = "\033[32m", "\033[31m", "\033[33m", "\033[0m", "\033[2m"


def _expand(fn):
    """@cases([...]) 로 붙은 파라미터를 풀어 (이름, 호출가능) 목록을 만든다."""
    params = getattr(fn, "_cases", None)
    if not params:
        return [(fn.__name__, fn)]
    out = []
    for p in params:
        label = p if isinstance(p, str) else repr(p)
        out.append((f"{fn.__name__}[{label}]", lambda p=p, f=fn: f(p)))
    return out


def main(argv: list[str]) -> int:
    # pytest 의 conftest.py 와 같은 격리: 외부 네트워크 차단, 실제 LLM 금지
    from mediator import llm as llm_mod
    from tests import net_guard
    net_guard.install()
    llm_mod.set_llm(llm_mod.DISABLED)
    filt = argv[1] if len(argv) > 1 else ""
    passed = failed = 0
    failures = []

    for modname in TEST_MODULES:
        try:
            mod = importlib.import_module(modname)
        except Exception:
            print(f"{R}모듈 로드 실패{X} {modname}")
            traceback.print_exc()
            failed += 1
            continue

        fns = [f for name, f in vars(mod).items()
               if name.startswith("test_") and inspect.isfunction(f)]
        if not fns:
            continue

        short = modname.split(".")[-1]
        if filt and filt not in short:
            continue
        print(f"\n{short}")

        setup = getattr(mod, "setup_function", None)
        for fn in fns:
            for label, call in _expand(fn):
                if filt and filt not in label and filt not in short:
                    continue
                try:
                    if setup:
                        setup(fn)
                    call()
                    print(f"  {G}PASS{X}  {label}")
                    passed += 1
                except Exception as exc:
                    print(f"  {R}FAIL{X}  {label}")
                    failed += 1
                    failures.append((label, traceback.format_exc()))

    print(f"\n{'─' * 60}")
    if failed:
        for label, tb in failures:
            print(f"\n{R}{label}{X}\n{D}{tb}{X}")
        print(f"{R}{failed} failed{X}, {G}{passed} passed{X}")
        return 1
    print(f"{G}{passed} passed{X}")
    return 0


def cases(params):
    """pytest.mark.parametrize 의 최소 대체물."""
    def deco(fn):
        fn._cases = params
        return fn
    return deco


if __name__ == "__main__":
    sys.exit(main(sys.argv))
