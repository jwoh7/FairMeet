"""발표 전 전체 점검. OS 무관하게 동작한다.

    python scripts/preflight.py            # 오프라인 항목만
    python scripts/preflight.py --live     # Google 토큰까지 확인
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 자식 Python 프로세스가 콘솔 코드페이지(예: Windows CP949)로 표준출력을
# 열어 유니코드 문자(–, —, ▸ 등) 출력 시 UnicodeEncodeError로 죽는 것을 방지.
_CHILD_ENV = {**os.environ, "PYTHONPATH": str(ROOT),
              "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def _run_child(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, capture_output=True,
                          encoding="utf-8", errors="replace",
                          env=_CHILD_ENV)

G, R, Y, X, D = "\033[32m", "\033[31m", "\033[33m", "\033[0m", "\033[2m"

_fail = 0
_warn = 0


def check(name: str, fn, warn_only: bool = False) -> bool:
    global _fail, _warn
    print(f"  {name:<44}", end="", flush=True)
    try:
        fn()
        print(f"{G}OK{X}")
        return True
    except Exception as exc:  # noqa: BLE001
        if warn_only:
            print(f"{Y}주의{X}")
            print(f"     {D}{exc}{X}")
            _warn += 1
        else:
            print(f"{R}실패{X}")
            print(f"     {D}{exc}{X}")
            _fail += 1
        return False


def main() -> int:
    live = "--live" in sys.argv
    print(f"\nFairMeet preflight  {D}(위치: {ROOT}){X}\n")

    print("기본")

    def _core_imports():
        for m in ("fastapi", "uvicorn", "httpx", "dotenv"):
            importlib.import_module(m)
    check("핵심 패키지 (fastapi, uvicorn, httpx)", _core_imports)

    def _google_imports():
        for m in ("googleapiclient", "google_auth_oauthlib"):
            importlib.import_module(m)
    check("Google 클라이언트 라이브러리", _google_imports, warn_only=not live)

    def _config():
        from common import config
        if config.using_dev_secrets():
            raise RuntimeError(
                ".env 가 없어 개발용 기본 비밀키를 쓰고 있습니다. "
                "데모는 되지만 실제 배포에는 부적합합니다.")
    check(".env 비밀키 설정", _config, warn_only=True)

    def _email():
        # 값은 출력하지 않는다. 비어 있는 항목의 '이름'과 상황만 알려준다.
        from common import config
        from mediator import notifier as nm
        if config.EMAIL_BACKEND == "smtp":
            nm.SmtpSettings.from_env()
        warns = nm.base_url_warnings()
        if warns:
            raise RuntimeError(" / ".join(warns))
    check("승인 요청 이메일 설정", _email, warn_only=True)

    def _secrets_not_tracked():
        import subprocess
        try:
            out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return          # git 저장소가 아니면 건너뜀
        bad = [l for l in out.splitlines()
               if l in (".env", "credentials.json") or l.startswith("tokens/token_")]
        if bad:
            raise RuntimeError(f"비밀 파일이 git 에 추적되고 있습니다: {bad}")
    check("비밀 파일이 git 에 없는지", _secrets_not_tracked)

    print("\n로직")

    def _tests():
        r = _run_child([sys.executable, "-m", "tests.run_tests"])
        if r.returncode != 0:
            tail = "\n     ".join(r.stdout.strip().splitlines()[-6:])
            raise RuntimeError(tail)
    check("테스트 스위트", _tests)

    def _demo():
        r = _run_child([sys.executable, "-m", "eval.demo_local"])
        if r.returncode != 0 or "COMMITTED" not in r.stdout:
            raise RuntimeError("오프라인 데모가 COMMITTED 에 도달하지 못했습니다")
    check("오프라인 데모 (Google 없이 전 구간)", _demo)

    def _compare():
        r = _run_child([sys.executable, "-m", "eval.compare"])
        if r.returncode != 0:
            raise RuntimeError("베이스라인 비교 실험 실패")
    check("베이스라인 비교 실험", _compare)

    def _db():
        import tempfile
        from mediator import store
        store.set_db_path(tempfile.mktemp(suffix=".db"))
        store.init_db()
    check("DB 스키마 생성", _db)

    print("\n자산")

    def _profiles():
        from agent.profile import Profile, validate_profile
        for k in ("a", "b", "c"):
            p = Profile.load(ROOT / "profiles" / f"{k}.json")
            errs = validate_profile(p)
            if errs:
                raise RuntimeError(f"profiles/{k}.json: {errs}")
    check("프로필 3종 검증", _profiles)

    def _fake():
        import json
        d = json.loads((ROOT / "profiles" / "fake_calendars.json")
                       .read_text(encoding="utf-8"))
        assert set(d) == {"a", "b", "c"}
    check("가짜 캘린더 데이터", _fake)

    def _dashboard():
        for name, least in (("index.html", 200), ("app.js", 5000), ("fairmeet.css", 3000)):
            p = ROOT / "dashboard" / name
            if not p.exists() or p.stat().st_size < least:
                raise RuntimeError(f"dashboard/{name} 이 없거나 비어 있습니다")
    check("대시보드", _dashboard)

    def _admin_login():
        # 값은 출력하지 않는다. 터널로 공개하기 전에 필요한 설정인지만 알려준다.
        from mediator import auth
        if not auth.is_configured():
            raise RuntimeError("관리자 로그인이 설정되지 않았습니다 (로컬 전용 모드). 터널로 공개하기 전에 "
                               "python -m scripts.set_admin_password 를 실행하세요.")
    check("관리자 로그인 (공개 전 필수)", _admin_login, warn_only=True)

    if live:
        print("\nGoogle 연동")

        def _creds():
            if not (ROOT / "credentials.json").exists():
                raise RuntimeError("credentials.json 이 없습니다")
        if check("credentials.json", _creds):
            for label in ("a", "b", "c"):
                def _tok(l=label):
                    import datetime as dt
                    from zoneinfo import ZoneInfo
                    from agent.calendar_client import GoogleCalendar
                    cal = GoogleCalendar(str(ROOT / "tokens" / f"token_{l}.json"))
                    now = dt.datetime.now(ZoneInfo("Asia/Seoul"))
                    cal.fetch_busy(["primary"], now, now + dt.timedelta(days=7))
                check(f"토큰 유효: {label}", _tok)

    print("\n" + "─" * 58)
    if _fail == 0:
        msg = f"{G}전체 통과{X}"
        if _warn:
            msg += f"  {Y}(주의 {_warn}건){X}"
        print(f"  {msg} — 시연 준비 완료")
        if not live:
            print(f"  {D}Google 연동까지 확인하려면: "
                  f"python scripts/preflight.py --live{X}")
        return 0

    print(f"  {R}{_fail}건 실패{X} — 시연 전에 해결하세요")
    print(f"  {D}토큰 실패라면: python scripts/bootstrap_tokens.py{X}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
