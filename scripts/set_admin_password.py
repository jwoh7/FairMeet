"""관리자 아이디와 비밀번호(해시)를 .env 에 기록한다.

    python -m scripts.set_admin_password            (아이디를 묻고, 비밀번호를 두 번 입력받는다)
    python -m scripts.set_admin_password --user me

비밀번호는 화면에 나타나지 않고 어디에도 저장되지 않는다. .env 에는 scrypt 해시만 쓰인다.
.env 의 다른 줄은 그대로 두고, 이 두 줄(FAIRMEET_ADMIN_USER, FAIRMEET_ADMIN_PASSWORD_HASH)만 바꾼다.
서버를 다시 시작해야 적용된다. 이 스크립트는 비밀번호도 해시도 출력하지 않는다.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mediator import auth  # noqa: E402

KEYS = ("FAIRMEET_ADMIN_USER", "FAIRMEET_ADMIN_PASSWORD_HASH")


def update_env_text(text: str, values: dict[str, str]) -> str:
    """text 안의 KEY=... 줄을 바꾸고, 없는 키는 끝에 추가한다. 다른 줄은 건드리지 않는다."""
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl) if text else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:export\s+)?([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in values:
            lines[i] = f"{m.group(1)}='{values[m.group(1)]}'"
            seen.add(m.group(1))
    if lines and lines[-1] == "":
        lines.pop()
    for k, v in values.items():
        if k not in seen:
            lines.append(f"{k}='{v}'")
    return nl.join(lines) + nl


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="관리자 아이디")
    ap.add_argument("--env", default=str(ROOT / ".env"), help=".env 경로")
    args = ap.parse_args(argv)

    user = (args.user or input("관리자 아이디: ")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._@-]{3,40}", user):
        print("아이디는 영문/숫자/._@- 3~40자여야 합니다.")
        return 2
    pw = getpass.getpass(f"비밀번호({auth.MIN_PASSWORD_LEN}자 이상): ")
    problem = auth.password_problem(pw, user)
    if problem:
        print(problem)
        return 2
    if getpass.getpass("비밀번호 다시 입력: ") != pw:
        print("두 번 입력한 비밀번호가 다릅니다.")
        return 2

    env_path = Path(args.env)
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    new = update_env_text(text, {KEYS[0]: user, KEYS[1]: auth.hash_password(pw)})
    fd, tmp = tempfile.mkstemp(dir=str(env_path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        os.replace(tmp, env_path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    print(f"관리자 '{user}' 의 로그인 정보를 {env_path.name} 에 기록했습니다 (비밀번호는 저장하지 않았습니다).")
    print("mediator(8000) 를 다시 시작하면 적용됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
