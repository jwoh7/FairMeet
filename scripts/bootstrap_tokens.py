"""사용자별 Google OAuth 토큰을 대화형으로 발급한다.

⚠️ 시연 전날 반드시 다시 실행할 것.
   OAuth 동의 화면이 '외부(External)' + '테스트(Testing)' 인 앱의
   refresh token 은 7일 후 만료된다.

사용법:
    python scripts/bootstrap_tokens.py           # a, b, c 전부
    python scripts/bootstrap_tokens.py a         # a 만
    python scripts/bootstrap_tokens.py --check   # 발급 없이 유효성만 확인
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.calendar_client import (  # noqa: E402
    CalendarUnavailable, GoogleCalendar,
)
from agent.profile import Profile  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
G, R, Y, X, D = "\033[32m", "\033[31m", "\033[33m", "\033[0m", "\033[2m"


def check_one(label: str) -> bool:
    """토큰으로 실제 FreeBusy 를 호출해 본다. 브라우저를 열지 않는다."""
    token = ROOT / "tokens" / f"token_{label}.json"
    if not token.exists():
        print(f"  {R}없음{X}   {label}: {token.name} 이 없습니다")
        return False
    try:
        cal = GoogleCalendar(str(token))
        now = dt.datetime.now(KST)
        busy = cal.fetch_busy(["primary"], now, now + dt.timedelta(days=7))
        n = len(busy.get("primary", []))
        print(f"  {G}유효{X}   {label}: 향후 7일 busy 구간 {n}건")
        return True
    except CalendarUnavailable as exc:
        print(f"  {R}만료{X}   {label}: {str(exc).splitlines()[0]}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  {R}오류{X}   {label}: {exc}")
        return False


def issue_one(label: str) -> bool:
    token = ROOT / "tokens" / f"token_{label}.json"
    prof_path = ROOT / "profiles" / f"{label}.json"
    name = label
    if prof_path.exists():
        name = Profile.load(prof_path).display_name

    print(f"\n{'─' * 58}")
    print(f"  {name} ({label}) 계정 인증 — 브라우저가 열립니다")
    print(f"  {D}'이 앱은 확인되지 않았습니다' 경고가 나오면{X}")
    print(f"  {D}고급 → FairMeet(으)로 이동(안전하지 않음) 을 클릭하세요{X}")
    print(f"{'─' * 58}")

    try:
        cal = GoogleCalendar(str(token))
        now = dt.datetime.now(KST)
        cal.fetch_busy(["primary"], now, now + dt.timedelta(minutes=1))
        print(f"  {G}✓{X} tokens/{token.name} 저장 완료")
        return True
    except CalendarUnavailable as exc:
        print(f"  {R}✗{X} {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  {R}✗{X} {exc}")
        return False


def main(argv: list[str]) -> int:
    (ROOT / "tokens").mkdir(exist_ok=True)

    if not (ROOT / "credentials.json").exists():
        print(f"{R}credentials.json 이 없습니다.{X}\n")
        print("Google Cloud Console 에서 받아 프로젝트 루트에 저장하세요:")
        print("  1. console.cloud.google.com → 프로젝트 생성")
        print("  2. API 및 서비스 → 라이브러리 → Google Calendar API → 사용")
        print("  3. OAuth 동의 화면 → 외부 → 테스트 사용자에 시연 계정 전부 등록")
        print("  4. 사용자 인증 정보 → OAuth 클라이언트 ID → "
              "애플리케이션 유형: 데스크톱 앱")
        print("  5. JSON 다운로드 → credentials.json 으로 저장")
        return 1

    args = [a for a in argv[1:] if not a.startswith("--")]
    check_only = "--check" in argv
    labels = args or ["a", "b", "c"]

    if check_only:
        print("토큰 유효성 확인 (브라우저를 열지 않습니다)\n")
        ok = sum(check_one(l) for l in labels)
        print(f"\n{ok}/{len(labels)} 유효")
        if ok < len(labels):
            print(f"{Y}만료된 토큰이 있습니다. "
                  f"python scripts/bootstrap_tokens.py 로 재발급하세요.{X}")
            return 1
        return 0

    ok = sum(issue_one(l) for l in labels)
    print(f"\n{'═' * 58}")
    print(f"  {ok}/{len(labels)} 발급 완료")
    if ok == len(labels):
        print(f"  {Y}⚠ 테스트 모드 앱의 토큰은 7일 후 만료됩니다.{X}")
        print(f"  {Y}  시연 전날 이 스크립트를 다시 실행하세요.{X}")
    return 0 if ok == len(labels) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
