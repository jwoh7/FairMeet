# FairMeet 구현 검토 및 실행 가이드 작성 요청 프롬프트

아래 프롬프트와 기존 FairMeet 기획·구현 초안 파일을 함께 Claude Code에 전달한다.

---

## Claude에게 전달할 프롬프트

너는 대학생 해커톤 프로젝트를 검증하고 실제 구현까지 안내하는 시니어 AI 시스템 아키텍트이자 백엔드 엔지니어다.

첨부 문서는 이전에 제안된 “개인 AI 에이전트들이 사용자의 비공개 일정과 선호를 대신 협상하고, 누적 양보를 보정하여 공정하게 약속을 잡는 FairMeet”의 기획·구현 초안이다.

기존 답변을 방어하거나 단순 요약하지 말고, 실제 구현 및 시연 가능성을 냉정하게 검토한 뒤 기술 오류를 바로잡은 최종 구현 청사진과 실행 가이드를 작성하라.

## 설명 수준에 관한 원칙

독자의 지식 수준을 의도적으로 초보자 수준으로 제한하거나 기술 내용을 단순화하지 마라. 설계 근거, 보안 문제, 프로토콜, 알고리즘, 예외 처리 등 필요한 기술적 깊이는 유지하라.

다만 모든 작업을 구체적이고 재현 가능하게 설명하라. 독자가 배경지식을 추측해야만 진행할 수 있는 설명은 피하고, 각 단계에서 다음을 명확히 제시하라.

- 무엇을 하는 단계인지
- 왜 필요한지
- 어느 프로그램·웹사이트·메뉴·터미널에서 수행하는지
- Windows PowerShell, VS Code 터미널 등 어떤 실행 환경을 기준으로 하는지
- 입력할 명령 또는 작성할 파일의 정확한 내용
- 명령 실행 전 필요한 조건
- 정상적으로 완료되었을 때 보여야 하는 결과
- 실패하기 쉬운 지점과 대표 오류의 해결 방법
- 비밀키·OAuth 토큰·개인정보를 어디에 저장하면 안 되는지
- 다음 단계로 넘어가기 위한 완료 조건

즉, 설명은 친절하고 구체적이어야 하지만 기술 수준이나 구현 범위를 임의로 낮추면 안 된다. 용어는 정확하게 사용하고, 어려운 용어는 삭제하지 말고 처음 등장할 때 짧게 정의하라.

## 프로젝트 목표

- 회사 팀과 친구 그룹에서 사용할 수 있는 일정 협상 서비스
- 각 사용자의 에이전트가 캘린더와 개인 제약을 로컬에서 분석
- 일정 제목이나 민감한 사유를 공개하지 않고 비용 또는 거부 여부만 교환
- 반복되는 약속에서 특정 사용자만 계속 양보하지 않도록 누적 부담을 보정
- 최종 확정은 사람의 승인을 받은 뒤 실제 Google Calendar에 등록
- 단순한 가상 시뮬레이션이 아니라 실제 API 연동 시연
- SDG 10.2를 주축으로 하고 SDG 8.8 또는 5.4를 보조 목표로 연결
- 해커톤 MVP 기준으로 구현 범위를 현실적으로 제한

팀 규모와 기간 정보가 없다면 “3인 팀, 48시간 해커톤, Python 경험 보유, OAuth 경험은 제한적”이라고 가정하고 명시하라. 사실과 가정을 구분하라.

## 먼저 바로잡아야 할 핵심 문제

### 1. 멀티에이전트와 중앙 최적화의 관계

현재 구조는 모든 에이전트의 전체 비용 벡터를 Mediator가 받은 뒤 중앙에서 최적해를 계산한다. 따라서 이를 완전한 탈중앙 협상이나 원본 정보를 공개하지 않는 유일한 구조라고 주장하면 안 된다.

다음 두 구조를 비교하고, 해커톤 MVP에 사용할 구조를 하나 선택하여 이후 설명과 코드에서 일관되게 적용하라.

- MVP: 로컬에서 효용을 계산하고 숫자화된 비용 벡터만 중앙 중재자에게 보내는 `privacy-minimized / federated utility architecture`
- 발전형: 제안된 슬롯의 비용 구간과 역제안만 단계적으로 공개하는 `progressive-disclosure negotiation`

MVP에서는 첫 번째 방식을 우선 검토하되, 전체 비용 벡터도 생활 패턴과 불가능 시간대를 추론할 수 있는 메타데이터라는 한계를 밝혀라. “완전한 프라이버시”, “제로 지식”, “민감정보 유출이 불가능하다” 같은 표현은 쓰지 마라.

에이전트가 반환한 `counter_indices`를 실제 다음 라운드 후보 선정에 사용하라. 역제안을 무시하고 기존 슬롯만 제외하는 로직을 협상이라고 설명하지 마라.

### 2. A2A 표준 관련 표현 수정

현재 `/rpc/negotiation/open` 등의 커스텀 REST 엔드포인트는 A2A 1.0 호환 구현이 아니다. 다음 요소가 빠져 있다.

- 올바른 Agent Card 필수 필드
- `supportedInterfaces`
- 인증 스킴
- JSON-RPC 2.0의 `jsonrpc`, `id`, `method`, `params`
- A2A의 `SendMessage` 또는 공식 Task/Message 구조
- HTTPS 및 요청 인증

따라서 MVP는 정확히 `A2A-inspired custom negotiation protocol`이라고 표현하라. 실제 A2A 호환 구현은 공식 스키마와 SDK를 적용하는 확장 단계로 분리하라.

검증 기준: [A2A 1.0 공식 명세](https://a2a-protocol.org/latest/specification/)

### 3. Google Calendar 권한과 개인정보 주장 수정

`calendar.freebusy`는 일정 제목 없이 busy 구간만 반환하지만, `calendar.events` 권한은 이벤트를 보고 수정할 수 있는 넓은 권한이다. 따라서 “API 권한 수준에서 일정 제목을 읽을 수 없다”는 주장은 사용하지 마라.

다음을 반영하라.

- 조회에는 `calendar.freebusy` 사용
- 쓰기에는 `calendar.events.owned` 적용 가능성 우선 검토
- `calendar.app.created`는 앱이 만든 보조 캘린더용이므로 primary 캘린더 기록과 혼동하지 않기
- 넓은 쓰기 권한이 필요한 경우 읽기 API를 호출하지 않는다는 애플리케이션 수준 통제로 정확히 설명
- 토큰을 Git 저장소, Docker 이미지, 화면 캡처, 발표 자료에 포함하지 않기
- 테스트 모드 외부 앱의 refresh token은 일반적으로 7일 후 만료될 수 있으므로 발표 직전 재발급 및 당일 preflight 수행
- FreeBusy 오류나 에이전트 타임아웃을 빈 시간으로 처리하지 않고 fail-closed 후 사람에게 넘기기

검증 기준:

- [Google Calendar FreeBusy](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query)
- [Google Calendar OAuth scopes](https://developers.google.com/workspace/calendar/api/auth)
- [Google OAuth 토큰 만료 규칙](https://developers.google.com/identity/protocols/oauth2)

### 4. 이벤트 중복 생성 문제

각 사용자 토큰으로 동일한 참석자 목록을 넣어 `events.insert()`를 여러 번 호출하면 중복 이벤트, 여러 Meet 링크, 중복 알림이 만들어질 수 있다.

다음 구조로 수정하라.

- 지정된 organizer 또는 요청자의 캘린더에 이벤트를 한 번만 생성
- 다른 사용자는 attendees로 초대
- Calendar insert에 협상 ID 기반의 멱등성 키 또는 자체 event ID 사용
- 재시도 시 동일 이벤트가 중복 생성되지 않게 처리
- `conferenceDataVersion=1` 적용
- 커밋 직전에 모든 참가자의 FreeBusy 재조회
- 충돌이 생겼으면 이벤트를 만들지 않고 재협상
- 이벤트 삽입 성공 후에만 양보 원장 갱신

검증 기준: [Google Calendar 이벤트 생성](https://developers.google.com/workspace/calendar/api/guides/create-events)

### 5. Slack 승인 플로우 수정

현재 승인 예시는 그대로 실행할 수 없다.

문제점:

- Slack 블록에 `block_id`를 설정하지 않았는데 이를 읽음
- 별도 action handler에서 지역 변수 `s`, `e`를 사용할 수 없음
- pending negotiation 상태가 저장되지 않음
- reject 처리와 승인 만료가 없음
- 중복 클릭과 Slack 재시도에 대한 멱등성이 없음
- 공개 채널에서 미승인자를 노출하면 권력 압박이 커질 수 있음

수정 방향:

- 버튼 `value`에는 `session_id`만 포함
- 슬롯, 참가자, 상태, 만료 시각은 DB에 저장
- 각 참가자에게 DM 또는 개인 모달로 승인 요청
- 채널에는 개인별 승인·거절 여부를 공개하지 않기
- 전원 승인, 거절, 만료, 캘린더 충돌을 명시적인 상태 전이로 처리
- slash command는 즉시 `ack()`하고 협상은 background task에서 수행
- Slack 요청 서명 검증
- 승인 TTL 적용

검증 기준: [Slack Bolt ack 공식 문서](https://docs.slack.dev/tools/bolt-python/concepts/acknowledge/)

### 6. 공정성 알고리즘 수정

다음 조건을 검증하고 코드와 설명에 반영하라.

- 양보 잔액은 `positive = 과거에 더 많이 희생하여 시스템이 배려해야 하는 상태`로 정의
- 의미가 혼동되는 `debt` 대신 `concession_balance` 또는 `burden_credit` 사용
- 잔액의 부호와 목적함수에 미치는 영향을 실제 숫자 예제로 설명
- 잔액에 decay와 상·하한 적용
- 제외된 슬롯 때문에 매 라운드 사용자의 최소 비용 기준이 다시 계산되지 않게 하기
- 최초 비용 행렬에서 regret을 한 번만 계산하고 이후에는 candidate mask만 변경
- 원장 갱신은 인간 승인과 Calendar commit 성공 뒤에만 수행
- 사용자별 비용 척도가 비교 가능하도록 정해진 비용 규칙과 범위 검증 적용
- 모든 시간을 HARD로 표시하거나 비용을 부풀리는 전략적 행동의 한계 명시
- 보호 시간 쿼터를 주장하려면 실제 쿼터 검증 구현을 제시하고, 구현하지 않으면 주장을 삭제

권장 목적함수는 다음과 같다. 수식, 설명, 구현의 부호가 서로 일치해야 한다.

```text
regret_i(s) = cost_i(s) - min_feasible_cost_i
adjusted_i(s) = regret_i(s) + lambda * concession_balance_i

selected_slot = lexicographic_min(
    max_i adjusted_i(s),
    sum_i regret_i(s)
)
```

양보 잔액이 큰 사용자가 왜 더 유리한 슬롯을 받게 되는지 2~3명의 작은 수치 예제로 증명하라.

### 7. 비용 함수 수정

- 기본 캘린더의 실제 busy 일정은 원칙적으로 HARD 충돌로 처리
- 기존 일정과 겹치는 슬롯을 단순히 `+45`로 허용하지 않기
- 옮길 수 있는 일정은 별도 flexible calendar 또는 명시적 movable hold에만 적용
- FreeBusy 구간 개수는 실제 회의 개수가 아니므로 `max_meetings_per_day`라고 부르지 않기
- `busy_minutes` 또는 `occupied_blocks` 같은 측정 가능한 이름 사용
- 자정을 넘는 보호 시간 처리
- `travel_min` 누락 및 외부 API 실패 처리
- 사용자의 출발 위치는 중앙 서버로 보내지 않고 개인 에이전트가 공개 약속 장소까지의 비용만 계산
- Distance Matrix는 Legacy이므로 신규 구현은 Routes API의 Compute Route Matrix를 검토
- 48시간 MVP에서는 이동시간 기능을 제외할 수 있으며, 제외 여부와 이유를 명시

검증 기준: [Google Routes Compute Route Matrix](https://developers.google.com/maps/documentation/routes/compute_route_matrix)

### 8. 개인정보·보안 주장 수정

“Mediator가 문자열을 받지 않으면 privacy leak이 0”이라는 지표는 삭제하라. 숫자 비용 벡터도 민감할 수 있다.

다음을 적용하라.

- 허용 필드 기반 payload schema test
- 일정 제목, 설명, 위치, 개인 사유, 원문 캘린더 ID가 Mediator로 넘어가지 않는지 검사
- 안정적인 무염 SHA-256 ID 대신 그룹별 HMAC pseudonym 또는 세션별 임시 ID
- agent endpoint에 Bearer token 또는 서명된 요청 적용
- nonce, timestamp, TTL을 이용한 replay 방지
- 비용 벡터와 협상 세션의 보관 기간 제한
- 개별 regret, balance, “이번에 가장 손해 본 사람”을 공개 채널에 표시하지 않기
- 데모 대시보드에서는 당사자가 동의한 가명 또는 합성 데이터만 사용
- “권력 구조를 제거한다”가 아니라 “개인 사유 공개 부담과 반복 양보를 줄이는 의사결정 보조 장치”라고 표현

### 9. LLM 역할 명확화

LLM은 다음 역할로 제한하라.

- “금요일 저녁은 알바” 같은 자연어를 구조화된 제약 후보로 변환
- JSON Schema 또는 Pydantic으로 결과 검증
- 사용자에게 변환 결과를 보여주고 확인받은 뒤 저장
- 파싱 실패 시 폼 입력으로 fallback
- 캘린더 제목이나 전체 일정 데이터를 LLM에 전송하지 않기

확정 시간 계산, 승인 판정, Calendar commit에는 LLM을 사용하지 말고 결정론적 코드로 처리하라.

### 10. 검증 코드 수정

다음을 포함한 현실적인 평가 설계를 제시하라.

- 동일한 데이터셋으로 first-fit, sum-min, minimax, history-aware minimax 비교
- 고정 시나리오와 반복 생성 시나리오를 분리
- 공통 가능 슬롯이 없는 사례 별도 평가
- `None` 및 실패 경로 처리
- HARD 위반 0건
- 중복 Calendar 이벤트 0건
- Calendar 변경 후 stale commit 0건
- 누적 regret의 분산 또는 Gini
- 최대 개인 regret
- 합의 성공률, 평균 라운드 수, 인간 재협상률
- 민감 필드 전송 여부
- API 장애, agent timeout, Slack 재전송 테스트

`Gini < 0.15`, `escalation 10~20%`처럼 근거 없는 목표치는 삭제하거나 초기 가설이라고 명시하라. 사용자 테스트에서는 결과 만족도, 프라이버시 신뢰, 기존 방식 대비 시간 절감, 거절 부담 감소를 함께 조사하라.

## 권장 상태 모델

다음 상태 전이를 바탕으로 DB와 처리 흐름을 설계하라.

```text
OPEN
-> COSTS_COLLECTED
-> AGENT_NEGOTIATING
-> PENDING_HUMAN_APPROVAL
-> RECHECKING_CALENDARS
-> COMMITTING
-> COMMITTED
```

실패 상태:

```text
REJECTED
EXPIRED
NO_FEASIBLE_SLOT
AGENT_UNAVAILABLE
STALE_CONFLICT
COMMIT_FAILED
```

각 상태 전이는 DB transaction과 멱등성 검사를 사용해야 한다.

## 개발 환경과 초기 설정 가이드 필수 요구사항

최종 답변에는 프로젝트 설계뿐 아니라, 빈 Windows 컴퓨터에서 저장소를 만들고 실제 시연을 실행하기까지의 초기 설정 과정을 별도 장으로 포함하라.

단순히 “Python과 API를 설치하세요”라고 쓰지 말고 다음 내용을 순서대로 설명하라.

### A. 전체 준비물과 계정

- 필요한 계정 목록
- 각 계정이 필요한 이유
- 무료 플랜, 유료 결제, 카드 등록 가능성이 있는 항목 구분
- Claude Code 사용 권한과 Anthropic API 사용료가 별개일 수 있다는 점
- Google Cloud, Slack, Anthropic Console 중 실제 MVP에 필요한 것과 선택 사항 구분
- API 키를 발급하기 전에 과금 한도와 사용량 알림을 설정하는 방법

가격과 무료 크레딧은 바뀔 수 있으므로 고정 금액을 단정하지 말고 공식 가격 페이지 확인을 안내하라.

### B. Windows 기본 도구 설치

다음 항목마다 공식 다운로드 위치, 설치 옵션, 설치 확인 명령, 예상 결과를 작성하라.

- VS Code
- Git for Windows
- Python 지원 버전
- Python 가상환경
- 필요할 경우 Node.js
- Claude Code VS Code 확장
- Claude Code CLI를 함께 사용할 경우 CLI 설치

Windows에서는 PowerShell을 기본 예시로 사용하라. Bash 전용 환경 변수 문법을 PowerShell 명령처럼 제시하지 마라. WSL을 대안으로 소개할 수 있지만 필수 경로와 대안 경로를 섞지 마라.

Claude Code의 현재 공식 설치 방법은 최신 공식 문서를 다시 확인한 후 작성하라. 오래된 npm 설치법을 무조건 기본값으로 제시하지 말고 현재 권장 설치법을 우선하라.

공식 기준:

- [Claude Code 설치와 초기 설정](https://code.claude.com/docs/en/setup)
- [VS Code에서 Claude Code 사용](https://code.claude.com/docs/en/vs-code)

### C. VS Code에서 Claude Code 사용하는 방법

다음을 실제 UI 흐름 기준으로 설명하라.

1. VS Code에서 프로젝트 폴더 열기
2. Extensions 화면에서 공식 `Claude Code` 확장 검색 및 설치
3. 확장 게시자가 올바른지 확인
4. Claude Code 패널 열기
5. 계정 로그인 및 인증
6. Plan/Manual/Auto/Edit automatically 모드의 차이
7. 처음에는 Plan 또는 Manual 모드를 권장하는 이유
8. 파일 또는 폴더를 `@`로 참조하는 방법
9. 첨부된 기획 문서와 이 프롬프트 파일을 함께 참조하는 방법
10. Claude에게 먼저 저장소를 읽고 계획을 제시하게 하는 방법
11. 계획 검토 후 파일 수정을 허용하는 방법
12. diff 검토, 수락, 거부, 되돌리기 흐름
13. VS Code 통합 터미널에서 테스트 명령 실행
14. Claude Code 세션 이어가기와 새 세션 시작

VS Code 확장만 사용할 때는 별도의 API 키가 필수인지, CLI를 별도 설치해야 하는지, Claude 계정 또는 Console 계정 중 무엇으로 로그인하는지 현재 공식 문서 기준으로 정확히 구분하라.

### D. Claude Code에 이 프롬프트 전달하는 실제 예시

다음과 같은 사용 흐름을 구체적으로 작성하라.

- 기존 기획 문서를 프로젝트의 `docs/original-plan.md`로 저장
- 이 요청 프롬프트를 `docs/implementation-review-request.md`로 저장
- VS Code Claude Code에서 두 파일을 `@`로 참조
- 첫 단계에서는 코드 수정 없이 검토만 요청
- 검토 결과를 `docs/review.md`에 작성하도록 요청
- 이후 승인된 설계에 따라 구현 계획을 생성
- 한 단계씩 구현하고 각 단계가 끝날 때 테스트하도록 요청

Claude Code에 처음 보낼 수 있는 실제 프롬프트 예시도 제공하라. 예시는 단순 문장 하나가 아니라 다음 조건을 포함해야 한다.

```text
@docs/original-plan.md와 @docs/implementation-review-request.md를 모두 읽어라.
아직 파일을 수정하지 말고 현재 저장소 구조와 요구사항을 분석하라.
P0/P1/P2 문제, 수정 아키텍처, 구현 순서, 검증 계획을 먼저 제시하라.
추측한 내용은 가정이라고 표시하고, 공식 API 문서로 확인해야 할 내용은 별도로 표시하라.
```

### E. Python 프로젝트 초기화

다음을 정확한 PowerShell 명령과 함께 설명하라.

- 프로젝트 폴더 생성 또는 Git clone
- VS Code에서 폴더 열기
- `.venv` 생성과 활성화
- Python interpreter 선택
- `requirements.txt` 또는 `pyproject.toml` 선택 근거
- 의존성 설치
- `.env.example`과 실제 `.env`의 차이
- `.gitignore`에 포함할 항목
- `credentials.json`, `token.json`, Slack secret, API key를 저장할 위치
- 환경 변수 로딩
- 첫 번째 smoke test 실행

명령마다 “어느 폴더에서 실행해야 하는지”를 표시하라.

### F. Google Calendar API 설정

Google Cloud Console의 현재 UI를 기준으로 다음 과정을 설명하라.

1. 프로젝트 생성
2. Google Calendar API 활성화
3. OAuth consent screen 구성
4. External/Testing 설정
5. 테스트 사용자 등록
6. 필요한 scope 선정
7. Desktop app 또는 Web app OAuth client 중 MVP 구조에 맞는 유형 선택
8. `credentials.json` 다운로드와 안전한 저장
9. 사용자별 최초 OAuth 인증
10. 사용자별 토큰 파일 분리
11. FreeBusy smoke test
12. 이벤트 insert smoke test
13. 토큰 만료 및 재인증 처리
14. 발표 당일 preflight 체크

각 단계에서 어떤 파일이 생성되고, 무엇을 Git에 올리면 안 되는지 명시하라.

### G. Slack 앱 설정

다음을 Slack 관리 화면의 메뉴 경로와 함께 설명하라.

- 앱 생성
- bot token scope 설정
- slash command 생성
- Interactivity 활성화
- Request URL 설정
- Signing Secret과 Bot Token 저장
- 로컬 서버를 Slack에서 접근하게 할 터널 선택
- 개발용 터널 URL이 바뀔 때 수정할 곳
- `/meet` smoke test
- 요청 서명 검증
- 3초 이내 ack와 background task 분리 확인
- 참가자 DM 승인 테스트

Slack을 48시간 MVP에서 제외할 수 있는 조건과, 웹 승인 화면으로 대체할 경우의 구현 차이도 제시하라.

### H. Anthropic API 또는 다른 LLM API 설정

Claude Code 자체 로그인과 FairMeet 애플리케이션이 호출하는 LLM API를 혼동하지 않도록 분리해 설명하라.

- Claude Code 사용 인증
- 애플리케이션용 Anthropic API key 발급
- API key 환경 변수 저장
- 최소 호출 테스트
- 비용 제한과 사용량 확인
- 자연어 제약 파서에만 API를 연결하는 방법
- API가 없어도 구조화 폼으로 시스템을 실행할 수 있는 fallback

애플리케이션에서 LLM 기능을 빼도 핵심 협상과 Calendar 연동이 동작하도록 설계하라.

### I. 서비스 실행 및 종료

다음을 포함하라.

- Mediator 실행 명령
- 개인 Agent 3개 실행 명령
- 서로 다른 포트 및 환경 파일 지정 방법
- Slack 또는 웹 UI 실행 명령
- 프로세스를 여러 VS Code 터미널로 나누는 방법
- 정상 기동 확인 URL 또는 health endpoint
- 로그에서 확인할 항목
- 올바르게 종료하는 방법
- 발표 전 전체 시스템을 한 번에 점검하는 스크립트 또는 명령

가능하면 `docker-compose` 없이 실행하는 기본 경로를 먼저 제시하고, Docker는 선택 사항으로 분리하라. Docker 사용 시에도 비밀 파일을 이미지에 복사하지 말고 read-only volume 또는 secret으로 주입하라.

### J. 단계별 오류 해결표

최소한 다음 오류의 원인, 확인법, 해결법을 표로 작성하라.

- `python` 또는 `claude` 명령을 찾을 수 없음
- PowerShell에서 가상환경 활성화가 차단됨
- VS Code가 잘못된 Python interpreter를 사용함
- Claude Code 로그인 창이 열리지 않음
- Google OAuth `redirect_uri_mismatch`
- Google OAuth `access_denied`
- refresh token 만료
- Calendar 401/403
- FreeBusy에 캘린더가 `notFound`로 표시됨
- Slack `dispatch_failed`
- Slack `invalid_auth`
- Slack 3초 timeout
- 터널 URL 변경
- 포트 충돌
- Agent timeout
- 공통 가능한 슬롯 없음
- Calendar commit 직전 일정 충돌
- 이벤트 중복 생성

## 구현 과정 작성 방식

구현 순서는 각각 독립적인 체크포인트를 가져야 한다. 각 단계마다 다음 형식을 사용하라.

```text
단계 이름
목표:
선행 조건:
수정/생성 파일:
실행 위치:
실행 명령:
기대 결과:
자동 테스트:
수동 확인:
대표 오류와 해결:
완료 조건:
다음 단계:
```

명령과 코드가 운영체제 또는 shell에 따라 다르면 Windows PowerShell을 먼저 제시하고 macOS/Linux 명령은 별도 섹션에 분리하라.

## 최종 답변 구성

다음 순서로 답하라.

1. 한 문단의 최종 실현 가능성 판정
2. P0/P1/P2 문제 목록
3. 수정된 제품 정의와 기존 일정 서비스 대비 차별점
4. 과장 없는 SDG 연결
5. 수정된 전체 아키텍처와 신뢰 경계
6. A2A-inspired MVP와 실제 A2A 확장안 구분
7. 수정된 프로토콜 메시지 예시
8. 수정된 공정성 알고리즘과 수치 예시
9. 협상 및 승인 상태 머신
10. 필요한 DB 스키마
11. Windows·VS Code·Claude Code 초기 설정
12. Python 프로젝트 생성과 비밀정보 관리
13. Google Calendar API 설정 및 실제 smoke test
14. Slack 앱 설정 및 실제 smoke test
15. 애플리케이션용 LLM API 설정과 fallback
16. 단계별 실제 구현 순서
17. 전체 서비스 실행·종료·점검 방법
18. 24시간·48시간·1주 버전의 범위
19. 자동 테스트 및 발표 리허설 계획
20. 오류 해결표
21. 심사위원에게 해도 되는 주장과 하면 안 되는 주장
22. 최종 Go/No-Go 체크리스트

코드 예시는 실행 가능한 수준으로 작성하라. 정의되지 않은 변수, `...`, 생략된 핵심 handler를 사용하지 마라. 전체 코드가 너무 길면 다음 네 부분을 완전하게 제시하라.

- 공정성 solver
- 협상 상태 저장 및 전이
- Slack 비공개 승인
- Calendar 재확인 및 단일 멱등 커밋

코드 작성 전에 프로젝트 구조와 파일별 책임을 먼저 제시하라. 코드와 명령은 서로 일치해야 하며, Bash용 환경 변수 문법과 PowerShell 문법을 섞지 마라.

마지막에는 반드시 다음을 명확히 결론 내려라.

- 현재 초안을 그대로 구현해도 되는가
- 무엇을 삭제해야 하는가
- 무엇을 먼저 구현해야 하는가
- 48시간 안에 안정적으로 시연 가능한 최소 범위는 무엇인가
- 시연 전날과 당일에 반드시 점검해야 할 항목은 무엇인가

답변 시점의 공식 문서를 확인하여 설치 방식, 메뉴명, API scope, OAuth 동작을 검증하라. 블로그나 검색 결과 요약보다 Anthropic, Google, Slack, A2A 공식 문서를 우선 인용하라. 확인하지 못한 내용은 단정하지 말고 “확인 필요”라고 표시하라.
