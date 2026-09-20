# FairMeet

개인 AI 에이전트가 **일정 제목이나 거절 사유를 공개하지 않고** 숫자화된 비용만
교환해서 약속 시간을 정하는 시스템. 반복되는 약속에서 특정 참여자에게 양보가
누적되지 않도록 과거 부담을 기록하고 다음 협상에 반영한다.

> **지금 바로 돌려보기** (Google 계정·네트워크 불필요, 30초)
> ```
> python -m eval.demo_local
> ```

---

## 이게 뭘 해결하는가

일정을 거절하려면 이유를 말해야 한다. 그리고 그 이유 — 돌봄, 통원, 아르바이트,
종교 활동 — 가 곧 취약성의 공개가 된다. 그래서 말하기 어려운 사람이 반복해서
양보한다.

FairMeet은 **거절에 사유 설명을 요구하지 않는 채널**을 만든다. 각자의 에이전트만
진짜 이유를 알고, 협상 테이블에는 0~100 사이 숫자와 거부권 플래그만 올라간다.
그리고 누가 지금까지 얼마나 양보했는지를 기억해서 다음 협상에 반영한다.

이 문제는 조직의 **포용적·참여적 의사결정(SDG 16.7)** 및 직급 때문에
불편을 표현하지 못하는 구성원의 **노동권과 안전한 근무 환경(SDG 8.8)**에
직접 연결된다. 돌봄 일정을 공개하지 않고 보호한다는 점에서는
**SDG 5.4**(무급 돌봄노동의 인정)도 보조적으로 연결된다.

---

## 5분 안에 시작하기

### 0단계 — 아무것도 설치하지 않고 확인

```bash
python -m eval.demo_local
```

`FakeCalendar`로 협상 → 승인 → 캘린더 커밋 전 구간이 돌아간다. 출력 끝에
양보 잔액 원장이 찍힌다. **발표 당일 Wi-Fi가 죽어도 이 경로로 시연할 수 있다.**

장애 주입도 해볼 수 있다:

```bash
python -m eval.demo_local --seed-balance   # 과거 양보 이력 → 제안 슬롯이 바뀜
python -m eval.demo_local --stale          # 승인 중 일정 충돌 → 이벤트 안 만듦
python -m eval.demo_local --fail-calendar  # 조회 실패 → fail-closed
python -m eval.demo_local --reject         # 1명 거절 → 2순위 재제안 → 전원 재승인 → 커밋
```

### 1단계 — 가상환경과 의존성

**PowerShell (Windows)**
```powershell
cd C:\dev\fairmeet
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> 실행 정책 오류(`이 시스템에서 스크립트를 실행할 수 없으므로...`)가 나면:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```

**bash (macOS / Linux / WSL)**
```bash
cd ~/dev/fairmeet
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2단계 — 비밀키

```powershell
Copy-Item .env.example .env          # PowerShell
python -c "import secrets; print(secrets.token_hex(32))"
```
```bash
cp .env.example .env                 # bash
python -c "import secrets; print(secrets.token_hex(32))"
```

출력값을 `.env`의 `FAIRMEET_GROUP_SECRET`에, 한 번 더 생성해서
`FAIRMEET_AGENT_TOKEN`에 넣는다.

### 3단계 — 테스트

```bash
pytest                       # pytest 가 있으면
python -m tests.run_tests    # 없어도 동작하는 대체 러너
```

전체 테스트가 통과해야 한다. 테스트는 실제 이메일이나 실제 Google Calendar 을
쓰지 않는다 (`tests/__init__.py` 가 `.env` 의 SMTP 설정이 테스트 프로세스에 들어오지
못하게 막는다).

### 4단계 — 웹으로 띄우기 (가짜 캘린더)

**PowerShell**
```powershell
$env:FAIRMEET_FAKE = "1"
uvicorn mediator.server:app --port 8000
```
**bash**
```bash
FAIRMEET_FAKE=1 uvicorn mediator.server:app --port 8000
```

<http://127.0.0.1:8000> 를 열고 입력창에 회의 요청을 문장으로 적는다 ([제품 화면](#제품-화면--보안--공개-db-스키마-v4) 참고).
조건이 명확하면 별도의 시작 버튼 없이 협상이 시작되고, 회의 상세 화면에 진행 단계와 참가자별 `응답 대기` / `응답 완료` 만
표시된다 (양보 잔액과 협상 로그는 로그인한 관리자가 시연 모드를 켰을 때만 보인다). **승인 링크는 화면에 나오지 않는다** — 참가자 각자의 이메일로만 간다
([승인 요청 이메일](#승인-요청-이메일) 참고). 기본 백엔드(`console`)는 메일을 실제로
보내지 않고 링크를 서버 로그에 남기므로, 로컬 확인용으로는 서버 터미널의
`[console 메일 — 실제 발송 아님]` 줄에서 링크를 열면 된다. 전원이 승인하면 이벤트가
만들어지고 잔액 원장이 갱신된다.

---

## 실제 Google Calendar 연동

여기부터는 **직접 해야 하는 계정 작업**이다. 코드는 이미 다 되어 있다.

### Google Cloud Console (10분)

1. <https://console.cloud.google.com> → 새 프로젝트 `fairmeet`
2. **API 및 서비스 → 라이브러리** → "Google Calendar API" → **사용**
3. **OAuth 동의 화면** → User Type **외부** → 앱 이름·이메일 입력
   - ⚠️ **게시 상태는 "테스트"로 둘 것.** 프로덕션 검증은 몇 주 걸린다.
4. **테스트 사용자 → ADD USERS** → 시연에 쓸 Gmail을 **전부** 등록
   - 여기에 없는 계정으로 로그인하면 `access_denied`가 난다
5. **사용자 인증 정보 → OAuth 클라이언트 ID**
   - 애플리케이션 유형: **데스크톱 앱** ← 웹 앱으로 하면 `redirect_uri_mismatch`
   - JSON 다운로드 → 프로젝트 루트에 **`credentials.json`** 으로 저장

### 토큰 발급

```bash
python scripts/bootstrap_tokens.py
```

계정마다 브라우저가 열린다. "이 앱은 확인되지 않았습니다" 경고에서
**고급 → FairMeet(으)로 이동** 을 클릭한다.

> ### ⚠️ 가장 위험한 함정
> **테스트 모드 외부 앱의 refresh token은 7일 후 만료된다.**
> 시연 전날 밤에 `python scripts/bootstrap_tokens.py` 를 **반드시 다시 실행**할 것.
> 당일 아침엔 `python scripts/bootstrap_tokens.py --check` 로 확인.

### 프로필 수정

공개 저장소에는 실제 이메일이 든 프로필을 포함하지 않는다. 예시 파일을 복사한 뒤
`email` 과 `user_key` 를 실제 Gmail 주소로 바꾼다.

```powershell
Copy-Item profiles\a.example.json profiles\a.json
Copy-Item profiles\b.example.json profiles\b.json
Copy-Item profiles\c.example.json profiles\c.json
```

macOS/Linux에서는 `cp profiles/a.example.json profiles/a.json`과 같은 방식으로 복사한다.

HARD 캘린더(`care`, `parttime`, `clinic`)는 Google Calendar에서 보조 캘린더를
만들고 그 **캘린더 ID**를 넣으면 된다. 여기에 들어간 일정은 **제목을 읽지 않고
거부권만 행사**한다 — 이게 이 프로젝트에서 기술적으로 가장 예쁜 부분이다.

### 실제 모드로 실행

터미널 4개가 필요하다. 각각 가상환경을 활성화한 뒤:

**PowerShell**
```powershell
# 터미널 1 — 중재자
uvicorn mediator.server:app --port 8000

# 터미널 2 — 에이전트 A
$env:FAIRMEET_PROFILE = "profiles\a.json"
$env:FAIRMEET_TOKEN   = "tokens\token_a.json"
uvicorn agent.server:app --port 8001

# 터미널 3 — B (포트 8002), 터미널 4 — C (포트 8003) 도 같은 방식
```

**bash**
```bash
uvicorn mediator.server:app --port 8000                           # 터미널 1
FAIRMEET_PROFILE=profiles/a.json FAIRMEET_TOKEN=tokens/token_a.json \
  uvicorn agent.server:app --port 8001                            # 터미널 2
```

> PowerShell은 `VAR=x command` 인라인 문법이 **동작하지 않는다.**
> `$env:VAR = "x"` 를 별도 줄에 쓸 것.

각 서버의 `/health` 로 확인한다.

---

## 승인 요청 이메일

협상이 후보 시간을 정하면 참가자 각자의 이메일(`profiles/{a,b,c}.json` 의 `email`)로
**개인 승인 요청**이 나간다. 메일에는 제안된 날짜·시간과 그 사람 전용 승인/거절 페이지
링크가 들어 있다.

### 링크는 이렇게 만들어진다

| 성질 | 방법 |
|---|---|
| 서명됨 | HMAC-SHA256. 키는 `FAIRMEET_GROUP_SECRET` 에서 용도별로 파생 |
| 참가자별로 다름 | 토큰에 세션 ID · 참가자 · 제안 번호 · 1회용 ID(`jti`) 가 들어 있음 |
| 일회용 | 응답을 기록하는 트랜잭션 안에서 `jti` 를 소비. 새로고침·재전송은 무시됨 |
| 만료됨 | 제안의 유효 시간(`FAIRMEET_APPROVAL_TTL_MIN`) 이 지나면 무효 |
| 이전 제안은 무효 | 다른 후보로 넘어가면 이전 링크는 서명이 맞아도 거부됨 |

무효한 링크는 이유를 구분하지 않고 항상 **"만료되었거나 이미 처리된 요청입니다"** 만
보여준다. 링크를 GET 으로 여는 것만으로는 아무것도 소비되지 않는다(메일 보안 스캐너가
미리 열어도 안전). 응답은 페이지의 버튼(POST)으로만 받는다.

토큰 본문은 DB 에 저장하지 않고 `jti` 만 저장한다. 그래서 DB 나 대시보드 API 에서는
링크를 되살릴 수 없다. (서버 접속 로그에는 요청 경로가 남고, `console` 백엔드는 링크를
로그에 찍는다 — 아래 주의 참고.)

### 설정 (`.env`)

| 변수 | 설명 |
|---|---|
| `FAIRMEET_EMAIL_BACKEND` | `smtp` 실제 발송 · `console` 로그에만 남김(기본) · `fake` 테스트용 |
| `FAIRMEET_PUBLIC_BASE_URL` | 메일 속 링크의 기준 주소. 아래 참고 |
| `FAIRMEET_SMTP_HOST` / `PORT` | SMTP 서버. 포트 587(STARTTLS, 기본) 또는 465(SSL) |
| `FAIRMEET_SMTP_USERNAME` / `PASSWORD` | 로그인 정보. 비밀번호는 코드·로그·DB·테스트 결과 어디에도 남지 않는다 |
| `FAIRMEET_SMTP_FROM` | 보내는 사람 주소 |
| `FAIRMEET_MAX_APPROVAL_ROUNDS` | 사람 승인 라운드 최대 횟수 (기본 3) |

발송은 `ApprovalNotifier` 인터페이스 뒤에 있다 (`mediator/notifier.py`). SMTP 는 TLS 가
없으면 로그인 정보를 보내지 않고 중단하며(평문 전송 없음), `FAIRMEET_GROUP_SECRET` 이
개발용 기본값이면 발송 자체를 거부한다(서명 키가 공개돼 있으면 링크를 위조할 수 있다).
`console` 백엔드는 실제 메일을 보내지 않는다. 대시보드 상단과 `/health` 에 현재 백엔드가
표시되니, `smtp` 인지 꼭 확인할 것.

### `FAIRMEET_PUBLIC_BASE_URL` 은 왜 필요한가

메일 속 링크는 `<FAIRMEET_PUBLIC_BASE_URL>/approve/<토큰>` 이 된다. 이 값을 비워 두면
`FAIRMEET_MEDIATOR_URL`(기본 `http://127.0.0.1:8000`)을 쓰는데, **`127.0.0.1` 과
`localhost` 는 "이 기기 자신"** 이라 받는 사람의 휴대폰이나 다른 컴퓨터에서는 열리지
않는다. 그래서 다음 경우에는 받는 사람이 열 수 있는 **HTTPS 공개 주소**가 필요하다.

- 승인하는 사람이 서버를 돌리는 컴퓨터가 아닌 **다른 기기·다른 네트워크**에서 링크를 열 때
- 휴대폰에서 메일을 보고 바로 누를 때
- 메일 서비스가 `http://` 링크에 경고를 띄우거나 막을 때 (공개 주소에는 HTTPS 를 쓴다)

서버 한 대에서 혼자 시험할 때는 localhost 로 충분하다. 실제 사람에게 보낼 때는 서버를
HTTPS 도메인으로 공개하거나 Cloudflare Tunnel · ngrok 같은 터널의 HTTPS 주소를 넣는다.

> ⚠️ **터널로 서버를 공개하면 승인 페이지와 함께 대시보드 화면 뼈대(`/`)와 로그인 화면도 인터넷에 보인다.**
> 관리자 기능(`/negotiate`, `/api/*`, 재발송 API 등)은 로그인한 관리자만 쓸 수 있고, 로그인 설정이 없으면 터널을 거친
> 요청은 전부 거절된다 ([관리자 로그인](#관리자-로그인)). 공개는 시연 시간 동안만 하는 것이 좋다. 서버 접속 로그에는 요청 경로(= 토큰)가 남는다. 토큰은 일회용이고
> 만료되지만 로그 파일은 비밀 파일처럼 다룬다.

### 발송 실패

발송이 실패하면 협상을 성공으로 위장하지 않는다. 세션은 승인 대기 상태로 남고,
대시보드에 해당 참가자가 **발송 실패**(오류 메시지 포함)로 표시되며 `/negotiate` 응답에
`needsAttention: true` 가 담긴다. **발송 실패·대기 건 다시 보내기** 버튼(또는
`POST /session/{id}/notifications/retry`)은 실패한 참가자에게만 **새 링크**를 발급해
보내고, 이미 발송된 사람에게는 다시 보내지 않는다.

## 거절과 재협상

한 명이라도 거절하면 세션이 끝나지 않고 **다음 후보로 다시 제안**한다.

```
제안 1순위 ─ 전원 승인 ───────────────▶ 재확인 → Google 커밋(이벤트 1개) → 잔액 1회 갱신
   │
   └ 1명이라도 거절 → 1순위 슬롯을 '시도함'으로 기록, 1순위 링크·응답 무효
        ├ 남은 후보 있음 & 라운드 여유 있음 → solve() 로 2순위 선택 → 새 링크·새 이메일 → 전원 재승인
        ├ 남은 후보 없음 ─────────────────────────────▶ CANDIDATES_EXHAUSTED
        └ 최대 승인 라운드 도달 ───────────────────────▶ MAX_APPROVAL_ROUNDS_REACHED
```

- 다음 슬롯은 처음 협상과 같은 공정성 목적함수(`common/fairness.py`의 `solve`), 같은
  regret 행렬, **현재 양보 잔액**, 같은 `lam` 으로 "아직 시도하지 않은 공통 가능 슬롯" 중에서
  고른다. 재제안에는 에이전트 왕복이 없다(비용은 이미 수집됐고, 이번에는 사람이 결정한다).
- 이전 슬롯의 승인은 새 슬롯에 재사용되지 않는다. 승인은 제안 번호별로 따로 저장된다.
- 거절 사유는 묻지도 저장하지도 않는다. 누가 거절했는지는 대시보드·로그·SSE 이벤트에
  실리지 않는다. 대시보드에는 `1순위 거절 → 2순위 재제안` 같은 진행만 보인다.
- **Google 이벤트와 양보 잔액은 전원 승인 + 재확인 + 커밋이 모두 성공했을 때만, 정확히
  한 번** 반영된다. 거절·발송 실패·종료에서는 바뀌지 않는다.
- 같은 슬롯은 두 번 제안되지 않는다. `proposals` 테이블의 `UNIQUE(session_id, slot_index)`
  가 DB 수준에서 막는다.
- 거절 처리(기록 → 슬롯 시도 표시 → 다음 슬롯 선택 → 새 제안 개설)는 하나의
  `BEGIN IMMEDIATE` 트랜잭션이다. 동시에 들어온 승인/거절, 두 명이 동시에 거절, 새로고침
  중복 요청 모두 한 번만 반영된다.
- 승인 대기 중 캘린더 충돌(`STALE_CONFLICT`)이 확인되어도 이벤트는 만들지 않는다
  (fail-closed). 충돌 슬롯을 '시도함'으로 제외하고 다음 후보로 새 승인 라운드를 열며,
  같은 횟수 제한이 적용된다.

### 종료 상태 (더 이상 이메일·이벤트·잔액 변경 없음)

| 상태 | 뜻 |
|---|---|
| `NO_COMMON_SLOT` | 처음부터 전원이 동시에 가능한 슬롯이 0개. 아무에게도 묻지 않고 즉시 종료 |
| `CANDIDATES_EXHAUSTED` | 제안할 수 있는 후보를 모두 시도했다(거절·충돌·제외) |
| `MAX_APPROVAL_ROUNDS_REACHED` | `FAIRMEET_MAX_APPROVAL_ROUNDS` 만큼 제안했지만 성사되지 않았다 |

전체 제안 횟수는 `min(공통 후보 수, 최대 승인 라운드)` 를 넘지 못한다. 후보가 남지 않으면
`CANDIDATES_EXHAUSTED` 가 우선한다. 종료 상태와 이유는 `sessions.state`·`fail_reason` 과
`transitions` 에 남고, 대시보드는 기술 용어 대신 다음 선택지를 보여준다.

1. **날짜 범위를 넓혀 새 협상 시작** — 탐색 기간을 7일 늘려(최대 28일) 새 세션 시작
2. **회의 시간을 줄여 새 협상 시작** — 회의 길이를 30분 줄여(최소 30분) 새 세션 시작
3. **이번 협상 종료** — 상태는 그대로 두고 종료 기록만 남긴다

### DB 마이그레이션

기존 `fairmeet.db` 는 지우지 않는다. 서버가 처음 기동할 때(`init_db`) 이전 스키마를
감지하면 **먼저 `fairmeet.bak-v0-<날짜-시각>.db` 로 통째 백업**한 뒤 한 트랜잭션으로
올린다(중간에 실패하면 아무것도 바뀌지 않는다). 백업 파일은 `.gitignore` 의 `*.db` 에
걸린다.

- `proposals`(시도한 슬롯), `approval_tokens`, `notifications` 테이블 추가
- `approvals` 의 기본키를 `(session_id, proposal_version, agent_id)` 로 변경 — 기존 행은
  모두 1번 제안의 것으로 옮긴다
- `sessions` 에 `proposal_version`, `lam` 컬럼 추가, 기존 제안은 1번 제안으로 소급 기록
- 이전 방식으로 발급된 링크는 더 이상 유효하지 않다. 예전 `PENDING_HUMAN_APPROVAL` 세션은
  기동 시 만료 시간이 지났다면 `EXPIRED` 로 정리된다

되돌리려면 서버를 모두 끄고 백업 파일을 `fairmeet.db` 로 복사하면 된다(그 뒤에는 이전 버전
코드를 써야 한다).

### Gmail SMTP 로 실제 발송하기

Gmail 은 일반 비밀번호로 SMTP 로그인을 허용하지 않는다. **앱 비밀번호**를 만들어 쓴다.

1. 보내는 용도의 Gmail 계정으로 <https://myaccount.google.com/security> 에 접속한다.
2. **2단계 인증**이 꺼져 있으면 켠다(앱 비밀번호의 전제 조건).
3. <https://myaccount.google.com/apppasswords> 로 이동한다. 메뉴에 안 보이면 검색창에
   "앱 비밀번호"를 입력한다.
4. 앱 이름에 `FairMeet` 처럼 알아볼 이름을 입력하고 **만들기**를 누른다.
5. 화면에 **16자리 비밀번호**가 딱 한 번 표시된다. 공백은 무시되므로 그대로 복사해도 된다.
6. `.env` 에 다음 항목을 채운다(값은 본인 것으로).
   - `FAIRMEET_EMAIL_BACKEND=smtp`
   - `FAIRMEET_SMTP_HOST=smtp.gmail.com`
   - `FAIRMEET_SMTP_PORT=587`
   - `FAIRMEET_SMTP_USERNAME=` 보내는 Gmail 전체 주소
   - `FAIRMEET_SMTP_PASSWORD=` 5 번의 16자리 앱 비밀번호
   - `FAIRMEET_SMTP_FROM=` 보통 위 Gmail 주소와 같은 주소
   - `FAIRMEET_PUBLIC_BASE_URL=` 받는 사람이 열 수 있는 HTTPS 주소
7. 서버(중재자)를 다시 시작한다.

앱 비밀번호가 안 보이는 경우: 2단계 인증이 꺼져 있음 · 고급 보호 프로그램 가입 계정 ·
회사/학교(Workspace) 관리자가 막아 둔 계정. 앱 비밀번호는 메일을 보낼 수 있는 권한이므로
비밀번호처럼 다루고, 유출이 의심되면 같은 페이지에서 삭제한 뒤 새로 만든다. 개인 Gmail 은
하루 발송량 제한이 있다(데모 용도로는 충분하다).

---

## 추가 기능 (DB 스키마 v3)

기존 전원 승인·거절 시 차순위 재제안 흐름은 그대로이고, 아래는 모두 그 위에 얹힌다.
기존 세션은 '전원 승인, 조건 없음'으로 남는다.

### 1. 자연어 회의 조건 — 확인 전에는 협상이 시작되지 않는다

```
POST /meeting-requests            {"text": "다음 주 중에 2시간 잡아줘. 월요일 오전과 금요일은 빼고 …"}
POST /meeting-requests/{id}/resolve   질문 답 / 조건 제거 / 수동 확인 / 강도·우선순위·소요 시간·기간 수정
POST /meeting-requests/{id}/confirm   '이 조건으로 협상 시작' (저장된 초안을 서버가 다시 검증한다)
```

대시보드의 **자연어로 회의 요청** 카드에서 같은 흐름을 쓴다. 문장은 `MeetingRequest`
(조건마다 `type / value / strength / priority / source_text / supported / explanation`)로
구조화된다.

- **hard** 조건을 어기는 슬롯은 후보에서 제외되고, **soft** 조건은 슬롯 선택에만 쓰이는 페널티가
  된다. 페널티는 개인 regret·양보 잔액에 섞이지 않는다.
- 자동 처리하는 것: 소요 시간, 날짜 범위, 제외·필수·선호 시간대, 빠른 시간 선호, 필수 참석자,
  승인 기준, 회의 전후 여유(에이전트가 `capabilities` 에 `buffer` 를 알릴 때만).
- **자동 확인 불가로 표시**하는 것 (임의로 처리하거나 무시하지 않는다): 대면·혼합, 장소·이동,
  접근성, 날씨·교통·영업시간, 반복 일정. 사용자가 '직접 확인'으로 받아들이거나 제거해야 시작할 수
  있다. 외부 정보 공급자는 `mediator.constraints.register_provider()` 로 나중에 붙인다.
- **확인 질문**: 모호한 표현(너무 늦은 저녁의 기준, 오전/오후 없는 시각, 어떤 일정이 '수업'인지 등)은
  임의로 정하지 않고 묻는다. 서로 모순되는 조건은 원인 조건을 짚어 알리고 해결 전에는 시작할 수 없다.
- **LLM 은 선택**이다. `ANTHROPIC_API_KEY` 가 있으면 LLM 이 해석하되 날짜·참여자·승인 기준·근거
  문장을 서버가 다시 검증한다. 키가 없거나 호출이 실패하면 규칙 기반으로 되돌아가고, 수동 협상은
  LLM 과 무관하다. 시스템 변경·명령·비밀값 요청으로 보이는 문장은 통째로 무시하고 알린다.

### 2. 필수 참석자와 승인 정족수

`POST /negotiate` 의 `approvalPolicy` (`{"mode": "min_count", "minCount": 2, "required": ["재원"]}`
등) 또는 자연어로 정한다. 전원 승인 / 최소 인원 / 최소 비율. 주최자는 항상 필수(이벤트가 그의
캘린더에 생긴다). 필수 참석자가 거절하거나 남은 사람이 모두 승인해도 정족수를 못 채우면 즉시 차순위
후보로 넘어간다. 정책은 세션 생성 때 한 번 저장되고 DB 트리거가 이후 변경을 막는다. 승인 메일과
페이지에 필수 여부와 기준이 나오며, 거절한 사람은 누구에게도 알리지 않는다(초대 목록도 전원 유지).

### 3. 확정된 회의의 변경 요청

확정되면 참석자에게 **서명·만료·일회용 링크**가 담긴 확정 안내 메일이 간다. 링크에서
`새 시간 재협상 / 나만 빠지고 기존 시간에 진행 / 회의 취소` 를 요청하면 즉시 아무것도 바뀌지 않고,
다른 참석자에게 **투표 메일**이 간다. 원래 회의를 확정한 것과 같은 규칙(전원 또는 필수+정족수)으로
통과하면 적용한다: 취소 = 이벤트 삭제, 제외 진행 = 같은 이벤트의 참석자 갱신, 재협상 = 새 세션이
전원(정책) 승인으로 확정되면 **같은 이벤트의 시간을 수정**(새 이벤트 없음). 거절·만료·실패하면 원래
일정이 그대로다. 세션마다 진행 중인 요청은 하나뿐이고(DB 부분 UNIQUE 인덱스), 지난 회의는 요청할 수
없으며, 요청자의 신원과 사유는 어디에도 남기거나 보여주지 않는다.
이 기능은 에이전트의 `/rpc/event/update`·`/rpc/event/cancel` 이 필요하므로 **에이전트 서버(8001~8003)를
새 코드로 다시 시작**해야 한다. 옛 에이전트는 기능을 광고하지 않아 '자동 확인 불가'/'지원하지 않음'으로
안전하게 처리된다.

### 4. 회의 준비: PDF 자료 다중 에이전트 분석 (선택, 일정 협상과 독립)

`POST /prep/analyze` (PDF 한 개) → 분석 → 비판 → (반박 라운드) → 종합. 요약, 주요 주장, 찬성·반대
근거, 위험·누락, 회의에서 결정할 질문, 추천 안건을 **쪽 번호 + 문서 원문 인용**과 함께 낸다. 서버가
인용이 그 쪽에 실제로 있는지 확인하고, 없으면 '문서에서 확인되지 않음'으로 표시한다. PDF 만 받고
크기·쪽수·글자 수를 제한하며(넘으면 잘라 쓰지 않고 거절), 손상·암호화·스캔본은 이유와 함께
거절한다. PDF 안의 지시·URL 접속·비밀값 요구 문장은 모델에 넘기기 전에 제거한다. 내부 추론은
요청·저장·노출하지 않고, 모델·토큰 예산·반박 라운드·제한 시간은 환경변수(`FAIRMEET_PREP_*`)다.
`pip install pypdf` 와 `ANTHROPIC_API_KEY` 가 필요하고, 없거나 실패해도 일정 기능에는 영향이 없다.
개념 참고: [muthuspark/multi-agent-debate](https://github.com/muthuspark/multi-agent-debate)
(MIT License) — 코드는 가져오지 않고 역할 분담·반박·판정 구조만 새로 구현했다.

---

## 제품 화면 · 보안 · 공개 (DB 스키마 v4)

### 화면
`http://127.0.0.1:8000/` 은 개발용 로그가 아니라 **회의를 요청하고 상태를 확인하는 화면**이다.
가운데의 입력창에 문장으로 요청하면 된다. 조건이 명확하고 자동으로 처리할 수 있으면 별도의 시작 버튼 없이 곧바로
협상이 시작되고, 애매하거나 충돌하거나 자동 처리할 수 없는 조건이 있으면 협상하지 않고 **이해한 내용과 확인 질문**을
보여 준다. 말하지 않은 회의 시간·날짜가 기본값으로 채워지는 경우도 한 번 확인을 받는다.

- 사용자 모드(기본): 진행 중·최근 회의, 응답 진행 상황, 확정된 시간, 일정 변경, 회의 자료. 양보 잔액·개인별 비용·
  에이전트 ID·원시 협상 로그·토큰·DB 정보·내부 상태 코드는 서버가 응답에 담지 않는다. 내부 상태는 사용자 문장으로 바뀐다
  (`AGENT_UNAVAILABLE` → "참가자의 일정을 확인하지 못했어요…").
- 시연 모드: 로그인한 관리자만 켜고 끈다 (URL·쿼리로는 켤 수 없다). 양보 잔액, 공정성 지표, **익명화된** 협상 단계·상태
  전이를 보여 준다. 표시만 바꾸며 협상 알고리즘·결과에는 영향이 없다. 개인 regret·일정 제목·거절 사유·이메일은 여기서도 없다.
- 화면 갱신은 폴링이다. Cloudflare Quick Tunnel 은 SSE 를 지원하지 않으므로 화면은 SSE 에 의존하지 않는다
  (`/events` 는 시연 모드에서만 열린다).
- 참가자가 이메일에서 여는 승인·거절·변경 투표·회의 자료 페이지는 휴대폰에서도 쓸 수 있게 만들었다. 메인 화면은
  1280px 이상 데스크톱에 맞췄고 PWA·서비스 워커·모바일 전용 내비게이션은 없다.

### 디자인과 TDS (Toss Design System) 판단
공식 TDS 는 **쓰지 않았다.** Toss 의 라이선스 문서는 TDS/UI Kit 의 사용을 "Apps in Toss 서비스 개발"로 한정하고, 그 밖의
프로젝트·제품·서비스에 쓰는 것, 컴포넌트를 복사·수정·재배포하는 것, 로고·브랜드 자산을 쓰는 것을 금지한다
(<https://developers-apps-in-toss.toss.im/design/prepare/figma-ui-license>). FairMeet 은 Apps in Toss 서비스가 아니므로
TDS 패키지·코드·이미지·아이콘·에셋·상표를 가져오지 않았고, Toss 와 관련된 서비스로 오인될 표현도 쓰지 않는다.
`dashboard/fairmeet.css` 는 여백, 정보 위계, 큰 제목과 짧은 문장, 카드·목록 행·배지·주요 버튼 같은 **일반적인 사용성
원칙**만 따른 자체 디자인(자체 색상, 겹치는 두 원 로고, 자체 컴포넌트)이고 "TDS 적용"이라고 표현하지 않는다.
(`tests/test_public_pages.py` 가 외부 자산·TDS 표기가 없음을 확인한다.)

### 관리자 로그인
Cloudflare Tunnel 로 공개하면 8000 서버가 외부에서 보인다. 그래서 **참가자 링크와 정적 화면 몇 개를 뺀 모든 경로는 기본으로
관리자 전용**이다 (새 경로를 추가해도 자동으로 잠긴다).

```powershell
.\.venv\Scripts\python.exe -m scripts.set_admin_password     # 아이디·비밀번호 입력 → .env 에 해시만 기록
```

- 자격정보는 환경변수(`FAIRMEET_ADMIN_USER`, `FAIRMEET_ADMIN_PASSWORD_HASH`)로만 받는다. 비밀번호는 scrypt 해시로만 두고
  기본 비밀번호는 없다 (12자 이상, 약한 비밀번호 거절).
- 세션 쿠키는 HttpOnly + SameSite=Strict, HTTPS 로 접속하면 Secure + `__Host-` 접두사. 세션 ID 는 서버 메모리의 무작위 값이라
  서버를 재시작하면 다시 로그인한다. 관리자 토큰을 URL 에 넣지 않는다.
- 상태를 바꾸는 요청은 CSRF 토큰과 Origin 검사를 통과해야 한다. 로그인은 IP 별·전체 별로 실패를 세어 잠근다
  (10분에 5회 / 30회). 아이디가 틀려도 비밀번호가 틀려도 같은 문구·같은 처리 시간이다.
- 자격정보가 없으면 **로컬 전용 모드**다: 이 컴퓨터에서 프록시 없이 직접 접속한 요청만 관리자 기능을 쓸 수 있고,
  터널을 거친 요청(프록시 헤더/공개 Host)은 전부 거절된다. 시연 모드는 로그인한 관리자만 쓸 수 있다.
- 보안 헤더(CSP: 인라인 스크립트·스타일 없음, 프레임 금지, nosniff, no-referrer), 오류 화면에 경로·스택트레이스 없음.

### 자연어 입력 방어
입력은 여러 겹을 지난다: 길이·형식 제한 → 의도 분류(회의 요청 / 질문 / 개인정보·권한 초과 / 시스템 공격,
`mediator/guard.py`) → 회의 조건만 허용된 구조로 추출 → 서버 재검증(스키마에 없는 필드 제거) → 관리자 인증과 CSRF →
협상 직전 정책 재검증 → 출력 단계 이메일·토큰·경로 제거 → 최소한의 보안 기록(길이와 짧은 해시만, 원문 없음, 30일) →
속도 제한(차단된 입력이 반복되면 잠시 멈춤). 다른 사람의 위치·일정·연락처·기밀 자료 요구, "내가 팀장이다" 같은 역할 주장,
정책 무력화, 이전 지시 무시, 비밀값·DB·파일 요구, SQL·셸·Python 실행, 내부/외부 URL 접속, 외부 전송 요구는 이유와 안전한
대안(예: "참가자에게 장소 선호를 물어볼까요?")만 알리고 처리하지 않는다. 회의 요청 안에 섞여 있으면 그 부분만 빼고
남은 조건을 보여 주되 자동으로 시작하지는 않는다. 같은 요청의 중복 제출은 idempotency key 로 하나의 회의만 만든다.

### 회의 자료 전달
분석 결과는 자동으로 아무에게도 가지 않는다. 회의가 **확정된 뒤** 주최자가 수신자 수를 확인하고 승인하면, **참석이 확정된
사람**(확정된 제안을 승인한 사람, 도중에 빠지지 않은 사람)에게만 이메일로 짧은 요약과 개인 서명 링크가 간다. PDF 원본은 저장하지도
첨부하지도 않는다. 링크는 서명·만료(기본 72시간)가 있고 (회의, 자료, 수신자)에 묶이며, 열 때마다 서버가 지금도 참석자인지
다시 확인한다 (빠지거나 회의가 취소되면 링크를 알아도 열리지 않는다). 관리자 화면에는 사람별 발송 성공/실패만, 참가자에게는
다른 사람의 수신 여부가 보이지 않는다. LLM 이 없으면 **규칙 기반의 제한적인 정리**임을 결과와 메일에 표시하고 다중 에이전트
분석이 끝난 것처럼 말하지 않는다.

### Cloudflare Tunnel (임시 시연용)
목적은 모바일 앱이 아니라 **참가자가 이메일 링크를 다른 기기에서 열 수 있게** 하는 것이다. Quick Tunnel 은 테스트·개발용이며
SLA 가 없고 SSE 를 지원하지 않으며 동시 요청이 200개로 제한된다.
공개하기 전에: ① `python -m scripts.set_admin_password` 로 관리자 로그인 설정 ② 전체 테스트 통과 ③ DB 백업 ④
`cloudflared tunnel --url http://127.0.0.1:8000` 로 나온 `https://*.trycloudflare.com` 주소를 `FAIRMEET_PUBLIC_BASE_URL` 에
넣고 8000 만 재시작 (8001~8003 에이전트는 로컬에만 두고 공개하지 않는다) ⑤ 공개 주소에서 로그인 화면이 뜨고,
`/api/meetings` 같은 경로가 로그인 없이는 401 인지, 휴대폰에서 서명된 승인 링크가 열리는지 확인.
**터널을 끄면 주소가 바뀌므로 이미 보낸 이메일 링크는 더 열리지 않는다.**
## 발표 전 점검

```bash
python scripts/preflight.py           # 오프라인 항목
python scripts/preflight.py --live    # Google 토큰까지
```

