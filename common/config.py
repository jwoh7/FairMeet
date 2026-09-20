"""환경변수 로딩. .env 가 없어도 import 자체는 실패하지 않도록 지연 검증한다."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:      # python-dotenv 미설치 시 OS 환경변수만 사용
    pass

# 데모/테스트에서 .env 없이도 돌아가도록 하는 기본값.
# 운영에서는 반드시 .env 로 덮어쓸 것.
_DEV_FALLBACK = {
    "FAIRMEET_GROUP_SECRET": "dev-only-insecure-secret-do-not-ship",
    "FAIRMEET_AGENT_TOKEN": "dev-only-insecure-token",
}


def get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    return default


def require(name: str) -> str:
    """운영 경로에서 쓰는 엄격한 조회. 값이 없거나 템플릿이면 즉시 실패."""
    val = os.environ.get(name)
    if not val or val.startswith("change-me"):
        raise RuntimeError(
            f"환경변수 {name} 가 설정되지 않았습니다.\n"
            f"  .env.example 을 .env 로 복사한 뒤 값을 채우세요.\n"
            f"  비밀키 생성: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    return val


def secret_or_dev(name: str) -> str:
    """데모는 통과시키되, 기본값을 쓰고 있으면 경고를 남긴다."""
    val = os.environ.get(name)
    if val and not val.startswith("change-me"):
        return val
    return _DEV_FALLBACK[name]


GROUP_SECRET = secret_or_dev("FAIRMEET_GROUP_SECRET")
AGENT_TOKEN = secret_or_dev("FAIRMEET_AGENT_TOKEN")
DB_PATH = get("FAIRMEET_DB", str(ROOT / "fairmeet.db"))
MEDIATOR_URL = get("FAIRMEET_MEDIATOR_URL", "http://127.0.0.1:8000")
APPROVAL_TTL_MIN = int(get("FAIRMEET_APPROVAL_TTL_MIN", "30"))
MAX_ROUNDS = int(get("FAIRMEET_MAX_ROUNDS", "3"))
LAMBDA_WORK = float(get("FAIRMEET_LAMBDA_WORK", "0.6"))
LAMBDA_SOCIAL = float(get("FAIRMEET_LAMBDA_SOCIAL", "1.0"))

# 사람 승인 라운드 상한. 거절될 때마다 다음 후보를 다시 제안하는 횟수를 제한한다.
# 실제 반복 횟수는 min(남은 후보 수, 이 값) 을 넘지 못한다.
MAX_APPROVAL_ROUNDS = max(1, int(get("FAIRMEET_MAX_APPROVAL_ROUNDS", "3")))

# 승인 요청 이메일. SMTP 비밀번호는 모듈 상수로 들고 있지 않고
# 발송 시점에 환경변수에서 읽는다 (notifier.SmtpSettings.from_env).
EMAIL_BACKEND = (get("FAIRMEET_EMAIL_BACKEND", "console") or "console").lower()
# 이메일 링크의 기준 주소. 비어 있으면 MEDIATOR_URL 을 쓴다.
PUBLIC_BASE_URL = (get("FAIRMEET_PUBLIC_BASE_URL") or MEDIATOR_URL)


# ---- 자연어 회의 조건 (선택 기능) ---------------------------------------------------------
# rules: 규칙 기반 해석만 쓴다 (외부 호출 없음).
# llm  : LLM 으로 해석한다. 키가 없거나 호출이 실패하면 규칙 기반으로 되돌아간다.
# auto : ANTHROPIC_API_KEY 가 있으면 llm, 없으면 rules.
# 어느 쪽이든 결과는 결정론적 검증을 다시 거치고, 사용자가 확인하기 전에는 협상이 시작되지 않는다.
NL_BACKEND = (get("FAIRMEET_NL_BACKEND", "auto") or "auto").lower()
NL_MAX_CHARS = max(50, int(get("FAIRMEET_NL_MAX_CHARS", "1000")))
DRAFT_TTL_MIN = max(1, int(get("FAIRMEET_DRAFT_TTL_MIN", "60")))

# LLM 호출 설정 (자연어 조건 해석, 회의 자료 분석 공용). 키 자체는 여기서 읽지 않는다 —
# SDK 가 발송 시점에 ANTHROPIC_API_KEY 를 읽는다.
LLM_MODEL = get("FAIRMEET_LLM_MODEL", "claude-opus-5")
LLM_TIMEOUT_S = float(get("FAIRMEET_LLM_TIMEOUT_S", "30"))
LLM_MAX_TOKENS = max(256, int(get("FAIRMEET_LLM_MAX_TOKENS", "2048")))

# ---- 확정된 회의의 일정 변경 ---------------------------------------------------------------
# 변경 투표/요청 링크의 유효 시간(분). 회의 시작 시각보다 길어지지는 않는다.
CHANGE_TTL_MIN = max(5, int(get("FAIRMEET_CHANGE_TTL_MIN", "1440")))
# 재협상이 새 시간을 찾을 기간(일). 내일부터 이 기간만큼 탐색한다.
CHANGE_SEARCH_DAYS = max(1, min(28, int(get("FAIRMEET_CHANGE_SEARCH_DAYS", "14"))))


# ---- 회의 준비: PDF 자료 다중 에이전트 분석 (선택 기능, 일정 협상과 독립) ---------------------------
# 개념 참고: muthuspark/multi-agent-debate (MIT) — 역할 분담 → 반박 라운드 → 판정. 코드는 가져오지 않았다.
PREP_ENABLED = (get("FAIRMEET_PREP_ENABLED", "1") or "1").lower() not in ("0", "false", "no", "off")
PREP_MAX_BYTES = max(1024, int(get("FAIRMEET_PREP_MAX_BYTES", str(5 * 1024 * 1024))))
PREP_MAX_PAGES = max(1, int(get("FAIRMEET_PREP_MAX_PAGES", "30")))
PREP_MAX_CHARS = max(1000, int(get("FAIRMEET_PREP_MAX_CHARS", "60000")))   # 추출 텍스트 상한 (넘으면 거절)
PREP_MODEL = get("FAIRMEET_PREP_MODEL")                                    # 없으면 FAIRMEET_LLM_MODEL
PREP_ROUNDS = max(0, min(3, int(get("FAIRMEET_PREP_ROUNDS", "1"))))        # 반박 라운드 수 (0 = 분석·비판·종합만)
PREP_MAX_TOKENS = max(256, int(get("FAIRMEET_PREP_MAX_TOKENS", "3000")))   # 호출 1회의 출력 상한
PREP_TOKEN_BUDGET = max(1000, int(get("FAIRMEET_PREP_TOKEN_BUDGET", "80000")))   # 분석 1건의 총 토큰 예산(추정)
PREP_TIMEOUT_S = max(5.0, float(get("FAIRMEET_PREP_TIMEOUT_S", "120")))    # 분석 1건의 제한 시간


# ---- 관리자 인증 · 보안 (Cloudflare Tunnel 로 공개하기 전에 설정) ---------------------------------
# 관리자 자격정보는 환경변수로만 받는다. 비밀번호 평문은 어디에도 두지 않고 해시만 둔다:
#   python -m scripts.set_admin_password     (터미널에서 비밀번호를 입력하면 .env 에 해시가 기록된다)
# 둘 다 비어 있으면 '로컬 전용 모드'다: 이 컴퓨터에서 직접(프록시 없이) 접속한 요청만 관리자 기능을 쓸 수 있다.
# (FAIRMEET_ADMIN_USER / FAIRMEET_ADMIN_PASSWORD_HASH 는 mediator/auth.py 가 요청 때마다 읽는다.)
ADMIN_SESSION_MIN = max(5, int(get("FAIRMEET_ADMIN_SESSION_MIN", "480")))       # 로그인 유지 최대 시간
ADMIN_IDLE_MIN = max(1, int(get("FAIRMEET_ADMIN_IDLE_MIN", "30")))              # 조작이 없으면 로그아웃
SECURITY_LOG_DAYS = max(1, int(get("FAIRMEET_SECURITY_LOG_DAYS", "30")))        # 보안 감사 기록 보관 기간

# ---- 회의 자료 공유 ---------------------------------------------------------------------------
MATERIAL_LINK_TTL_H = max(1, int(get("FAIRMEET_MATERIAL_LINK_TTL_H", "72")))    # 자료 열람 링크 유효 시간
MATERIAL_RETENTION_DAYS = max(1, int(get("FAIRMEET_MATERIAL_RETENTION_DAYS", "14")))   # 분석 결과 보관 기간 (PDF 원본은 저장하지 않는다)


def using_dev_secrets() -> bool:
    """preflight 에서 경고를 띄우기 위한 확인용."""
    return GROUP_SECRET == _DEV_FALLBACK["FAIRMEET_GROUP_SECRET"]
