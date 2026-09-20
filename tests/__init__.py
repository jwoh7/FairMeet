"""테스트 패키지.

테스트는 절대 실제 이메일을 보내지 않는다. common.config 가 .env 를 읽기 *전에*
(dotenv 는 이미 존재하는 환경변수를 덮어쓰지 않는다) SMTP 설정을 빈 값으로
못 박고 백엔드를 fake 로 고정한다. 나중에 .env 에 실제 SMTP 비밀번호가 들어가도
테스트 프로세스는 그 값을 볼 수 없다.

같은 이유로 관리자 자격정보, 공개 주소, LLM 키도 비워 둔다: 사용자가 .env 에 실제 값을 넣어도 테스트는
'로컬 전용 모드' + localhost 링크 + LLM 없음 상태에서 항상 같은 결과를 낸다. 관리자 로그인 테스트는
필요한 값을 그 테스트 안에서 직접 넣는다.
"""
import os

for _name in ("FAIRMEET_SMTP_HOST", "FAIRMEET_SMTP_PORT", "FAIRMEET_SMTP_USERNAME",
              "FAIRMEET_SMTP_PASSWORD", "FAIRMEET_SMTP_FROM",
              "FAIRMEET_ADMIN_USER", "FAIRMEET_ADMIN_PASSWORD_HASH",
              "FAIRMEET_PUBLIC_BASE_URL", "ANTHROPIC_API_KEY"):
    os.environ[_name] = ""
os.environ["FAIRMEET_EMAIL_BACKEND"] = "fake"

from mediator import auth as _auth  # noqa: E402  (환경변수를 비운 뒤에 import 한다)

_auth.trust_test_client()           # TestClient 를 '이 컴퓨터에서 직접 접속'으로 취급 (테스트 패키지만 호출)
