# FairMeet 구현 청사진 및 실행 가이드

> 이 문서는 기존 FairMeet 기획 초안에 대한 기술 검토 결과이자, 빈 Windows 컴퓨터에서 실제 시연까지 도달하기 위한 실행 가이드입니다.
>
> **작성 기준일:** 2026-09-19
> **검증 방법:** Google, Slack, A2A, Anthropic 공식 문서 직접 확인 (각 섹션에 출처 명시)
> **확인하지 못한 항목은 "⚠️ 확인 필요"로 표기했습니다.**

## 전제 조건과 가정

프롬프트에 팀 규모·기간 정보가 없으므로 다음을 **가정**하고 전체 문서를 작성합니다. 사실이 아니면 범위를 다시 잡아야 합니다.

| 항목 | 값 | 구분 |
|---|---|---|
| 팀 규모 | 3인 | **가정** |
| 개발 기간 | 48시간 해커톤 | **가정** |
| Python 경험 | 보유 | **가정** |
| OAuth 경험 | 제한적 | **가정** |
| 개발 환경 | Windows + VS Code + PowerShell | **가정** |
| 시연 방식 | 실제 Google 계정 3개로 라이브 시연 | 요구사항 (사실) |
| SDG 주축 | 10.2, 보조 8.8 / 5.4 | 요구사항 (사실) |

---

# 1. 최종 실현 가능성 판정

**판정: 범위를 줄이면 48시간 내 실제 시연 가능. 단, 현재 초안을 그대로 구현하면 실패합니다.**

핵심 협상 루프(로컬 비용 계산 → 비용 벡터 교환 → 공정성 최적화 → 인간 승인 → 실제 Google Calendar 이벤트 생성)는 기술적으로 평이하며, 사용하는 API는 모두 무료 구간에서 동작하고 문서화도 충분합니다. 병목은 알고리즘이 아니라 **OAuth 설정, 토큰 수명, 이벤트 중복 생성, 승인 상태 관리** 네 가지 실무 지점이며, 이들은 모두 해결책이 알려져 있습니다. 반면 초안이 내세운 주장 중 상당수는 사실과 다릅니다. 구조는 완전 탈중앙 협상이 아니라 **중앙 중재자를 둔 연합형 효용 계산(federated utility)** 이고, 커스텀 REST 엔드포인트는 A2A 1.0 호환이 아니며, `calendar.events` 권한을 요청하는 이상 "API 권한 수준에서 일정 제목을 못 읽는다"는 말은 성립하지 않습니다. 이 주장들을 정직한 형태로 고쳐 쓰는 것이 구현 작업만큼 중요합니다 — 심사위원 중 한 명이라도 Google API 문서를 아는 사람이라면 즉시 무너지는 지점이기 때문입니다. **48시간 목표는 "2인 협상 + 실제 캘린더 커밋 + 웹 승인 + 공정성 비교 실험"이고, Slack·이동시간·LLM 파서는 전부 선택 사항으로 내려야 합니다.**

---

# 2. P0 / P1 / P2 문제 목록

## P0 — 이걸 고치지 않으면 시연이 실패하거나 거짓 주장을 하게 됨

| # | 문제 | 현재 초안 | 수정 |
|---|---|---|---|
| P0-1 | **이벤트 중복 생성** | 사용자 3명 토큰으로 각각 `events.insert()` 호출 → 이벤트 3개, Meet 링크 3개, 알림 3중 | organizer 1명 캘린더에만 1회 insert, 나머지는 `attendees`. 세션 ID 기반 고정 event ID로 멱등성 확보 |
| P0-2 | **Slack 승인 코드 실행 불가** | `block_id` 미설정인데 읽음, 핸들러에서 지역변수 `s`/`e` 참조, 상태 미저장 | 상태를 DB에 저장, 버튼 `value`에 `session_id`만, 별도 핸들러에서 DB 조회 |
| P0-3 | **regret 기준선이 라운드마다 변동** | 제외 슬롯이 생기면 `min_feasible_cost`가 재계산되어 공정성 기준이 흔들림 | 최초 비용 행렬에서 regret을 **한 번만** 계산하고, 이후에는 candidate mask만 변경 |
| P0-4 | **기본 캘린더 충돌을 `+45`로 허용** | 이미 잡힌 회의 위에 새 회의를 배치할 수 있음 | primary busy는 원칙적으로 HARD. 이동 가능한 일정은 명시적 flexible 캘린더에만 |
| P0-5 | **refresh token 7일 만료 미고려** | 언급 없음 | Testing + External 앱의 refresh token은 7일 후 만료. 발표 직전 재인증 필수 |
| P0-6 | **원장 갱신 시점 오류** | 합의 즉시 갱신 | 인간 승인 + Calendar commit 성공 후에만 갱신 |
| P0-7 | **stale commit** | 커밋 직전 재확인 없음 | 커밋 직전 전원 FreeBusy 재조회, 충돌 시 재협상 |

## P1 — 주장과 사실이 어긋나는 지점 (심사에서 공격받음)

| # | 문제 | 수정 |
|---|---|---|
| P1-1 | "완전 탈중앙", "원본 비공개 유일 구조" | 중앙 중재자 구조임을 인정. `privacy-minimized federated utility architecture`로 정확히 표현 |
| P1-2 | "A2A 호환 구현" | `A2A-inspired custom negotiation protocol`로 표현. 실제 A2A 1.0 적용은 확장 단계 |
| P1-3 | "API 권한 수준에서 제목 못 읽음" | `calendar.events`는 넓은 권한. **애플리케이션 수준 통제**라고 정확히 서술 |
| P1-4 | "privacy leak = 0" 지표 | 삭제. 숫자 비용 벡터도 생활 패턴 추론이 가능한 메타데이터 |
| P1-5 | `counter_indices`를 무시 | 실제 다음 라운드 후보 선정에 반영 |
| P1-6 | 보호시간 쿼터 주장, 구현 없음 | 쿼터 검증을 구현하거나 주장을 삭제 |
| P1-7 | `debt`라는 혼동되는 이름 | `concession_balance`로 변경, 부호 의미를 문서화 |
| P1-8 | `max_meetings_per_day` | FreeBusy 구간 수 ≠ 회의 수. `occupied_blocks` / `busy_minutes`로 개명 |
| P1-9 | "권력 구조를 제거한다" | "개인 사유 공개 부담과 반복 양보를 줄이는 의사결정 보조 장치" |
| P1-10 | `Gini < 0.15`, `escalation 10~20%` 목표치 | 근거 없음. "초기 가설"로 명시하거나 삭제 |

## P2 — 있으면 좋지만 48시간에는 빼도 되는 것

| # | 항목 | 판단 |
|---|---|---|
| P2-1 | 이동시간 (Routes API) | **제외.** 출발지가 개인정보이고, Distance Matrix는 Legacy이며, 신규 Routes API 연동은 반나절을 먹음 |
| P2-2 | Slack 연동 | **선택.** 터널 URL·서명검증·3초 제한이 전부 실패 지점. 웹 승인 페이지로 대체 |
| P2-3 | LLM 자연어 제약 파서 | **선택.** 구조화 폼으로 100% 대체 가능해야 함 |
| P2-4 | A2A 공식 SDK | **제외.** 확장 계획으로만 서술 |
| P2-5 | HMAC pseudonym, nonce/replay 방지 | 구현하면 가점. 단순 Bearer 토큰만이라도 필수 |
| P2-6 | 3인 이상 동시 협상 | 2인으로 먼저 완성 후 3인 확장 |

---

# 3. 수정된 제품 정의와 차별점

## 제품 정의 (수정본)

> **FairMeet은 각 참여자의 개인 에이전트가 자신의 캘린더와 제약을 로컬에서 분석하여, 일정의 제목이나 사유를 공개하지 않고 숫자화된 비용만 교환함으로써 약속 시간을 정하는 의사결정 보조 시스템입니다. 반복되는 약속에서 특정 참여자에게 양보가 누적되지 않도록 과거 부담을 기록하고 다음 협상에 반영하며, 최종 시간은 반드시 전원의 명시적 승인을 받은 뒤에만 실제 캘린더에 기록됩니다.**

의도적으로 쓰지 않은 표현: "완전 탈중앙", "제로 지식", "프라이버시 보장", "권력 구조 제거", "유출 불가능".

## 기존 서비스 대비 차별점

| 축 | Reclaim / (구)Clockwise / Motion | FairMeet |
|---|---|---|
| 정보 모델 | 중앙 서비스가 전원 캘린더 **내용**을 열람 | 중앙 중재자는 **비용 벡터만** 수신 (제목·설명·위치·사유 미전송) |
| 조직 경계 | 단일 워크스페이스 내부 | 에이전트가 각자 독립 프로세스 → 조직·계정 경계를 넘어 확장 가능한 구조 |
| 목적함수 | 총 효율 (집중시간 최대화, 회의 압축) | **분배 공정성** (최대 개인 regret 최소화 + 누적 부담 보정) |
| 반복 상황 | 매 회의를 독립적으로 최적화 | 이전 라운드의 양보 잔액을 다음 라운드 목적함수에 반영 |
| 확정 권한 | 서비스가 자동 재배치 | 전원 승인 없이는 커밋하지 않음 (HITL 강제) |

**정직한 단서:** 중앙 중재자를 두는 이상 "타 서비스가 구조적으로 불가능한 일"을 하는 것은 아닙니다. 차이는 **목적함수와 노출 최소화 설계**이며, 이것이 이 프로젝트의 실질적 기여입니다. 실제 탈중앙 협상은 §6의 확장 단계에 해당합니다.

---

# 4. 과장 없는 SDG 연결

## 주축 — SDG 10.2 (연령·성별·장애·인종·출신·경제적 지위 등과 무관한 사회·경제·정치적 포용)

일정 협상에서 시간을 양보하는 쪽은 대체로 정해져 있습니다. 거절하려면 이유를 말해야 하고, 그 이유(돌봄, 통원, 아르바이트, 종교 활동)가 곧 취약성의 노출이 되기 때문입니다. FairMeet은 **거절에 사유 설명을 요구하지 않는 채널**을 제공하여 이 비대칭을 줄입니다.

**연결 강도: 강함.** 시스템의 핵심 메커니즘(사유 미공개 거부권)이 SDG 목표와 직접 대응합니다.

## 보조 — SDG 8.8 (노동권 보호와 안전한 노동환경)

업무 일정 결정에서 직급이 낮거나 고용이 불안정한 구성원은 사실상 거부권이 없습니다. 하드 제약과 전원 승인 요건은 최소한의 시간 자기결정권을 제도화합니다.

**연결 강도: 중간.** 시스템이 직접 노동권을 보호하는 것은 아니고, 협상 절차를 대칭화할 뿐입니다.

## 보조 — SDG 5.4 (무급 돌봄노동의 인정)

돌봄 책임은 성별에 따라 비대칭적으로 분포하며, 이는 일정 양보의 비대칭으로 이어집니다. 양보 잔액 원장은 이 누적 부담을 **측정 가능한 수치로 가시화**합니다.

**연결 강도: 중간.** 돌봄 부담 자체를 줄이지는 않습니다. 부담이 일정 결정에 미치는 영향을 기록하고 보정할 뿐입니다.

## 하지 말아야 할 SDG 주장

- ❌ SDG 13 (기후) — 이동시간 기능을 뺐으므로 탄소 감축 주장 불가
- ❌ SDG 3 (건강) — 회복시간 보호는 부수효과이며 건강 개선 근거 없음
- ❌ SDG 16 (제도) — 과도한 확대 해석

---

# 5. 수정된 전체 아키텍처와 신뢰 경계

## 5.1 아키텍처 선택

두 구조를 비교하고 **MVP는 (A)를 채택**합니다.

### (A) Privacy-minimized / federated utility architecture — **MVP 채택**

각 에이전트가 로컬에서 효용(비용)을 계산하고, 숫자화된 비용 벡터 전체를 중앙 중재자에게 전송합니다. 중재자가 최적화를 수행합니다.

- **장점:** 라운드 수가 적어 48시간에 완성 가능. 최적해가 전역적으로 보장됨. 디버깅이 쉬움.
- **한계 (반드시 명시):** 비용 벡터 전체는 **생활 패턴을 추론할 수 있는 메타데이터**입니다. 매주 화요일 14시가 항상 HARD라면 정기 일정의 존재가 드러나고, 평일 오전이 전부 고비용이면 근무 형태를 짐작할 수 있습니다. 제목은 감췄지만 **형태는 감추지 못합니다.**
- 따라서 "프라이버시가 보장된다"가 아니라 **"노출되는 정보의 양을 최소화했다"** 가 정확한 서술입니다.

### (B) Progressive-disclosure negotiation — 발전형

중재자가 슬롯을 제안하면 각 에이전트가 해당 슬롯에 대해서만 비용 **구간**(예: low/mid/high/veto)과 역제안을 반환합니다. 전체 벡터는 절대 노출되지 않습니다.

- **장점:** 노출 최소화가 훨씬 강력. 진짜 "협상"에 가까움.
- **단점:** 수렴 보장이 약하고 라운드가 많이 필요. 최적성 손실. 48시간에는 위험.
- **위치:** 발표에서 "다음 단계"로 제시하고, 가능하면 시뮬레이션 비교만 보여주세요.

## 5.2 구성도와 신뢰 경계

```
┌─ 신뢰 경계 1: 사용자 개인 디바이스/프로세스 ──────────────────┐
│  Agent A (:8001)          Agent B (:8002)                     │
│  ├ token_a.json           ├ token_b.json                      │
│  ├ profile_a.json         ├ profile_b.json                    │
│  └ cost.py (로컬 계산)    └ cost.py                           │
│         │                        │                            │
│         │  ↕ Google Calendar API (freebusy / events)          │
└─────────┼────────────────────────┼────────────────────────────┘
          │                        │
    [비용 벡터만]            [비용 벡터만]        ← 허용 필드 스키마로 강제
    Bearer 토큰 + nonce      Bearer 토큰 + nonce
          │                        │
┌─────────▼────────────────────────▼────────────────────────────┐
│  신뢰 경계 2: Mediator (:8000)                                 │
│  ├ 공정성 solver (regret + concession_balance)                │
│  ├ 상태 머신 + SQLite (sessions / ledger / approvals)         │
│  └ 웹 승인 페이지 (참가자별 1회용 토큰 링크)                   │
└───────────────────────────────────────────────────────────────┘
          │
    [organizer 토큰으로 단 1회 insert]
          ▼
    Google Calendar (organizer primary)
```

### 신뢰 경계를 넘는 것 / 넘지 않는 것

| 데이터 | Agent 내부 | Mediator로 전송 | 근거 |
|---|---|---|---|
| 일정 제목·설명·위치 | ✅ 보유 안 함 (FreeBusy만 조회) | ❌ | FreeBusy API는 busy 구간만 반환 |
| 원본 캘린더 ID | ✅ | ❌ | 이메일 주소가 노출되므로 차단 |
| 개인 제약 사유 | ✅ | ❌ | 스키마에 필드 자체가 없음 |
| 슬롯별 비용 정수 | ✅ | ✅ | 협상에 필수 |
| 거부권 플래그 | ✅ | ✅ | 협상에 필수 |
| 그룹별 가명 ID | — | ✅ | HMAC(group_secret, user_id) |
| organizer 이메일 | — | ✅ | 이벤트 생성에 필요 |

**⚠️ 주의:** Mediator가 organizer 토큰을 보유하는 구조는 신뢰 경계를 약화시킵니다. MVP에서는 organizer의 **Agent가** 커밋을 수행하고 Mediator는 "커밋하라"는 명령만 보내는 방식이 더 깨끗합니다. 본 문서는 후자를 채택합니다.

---

# 6. A2A-inspired MVP와 실제 A2A 확장안

## 6.1 왜 현재 구현이 A2A가 아닌가

A2A 1.0 명세에 비추어 초안의 `POST /rpc/negotiation/open` 같은 엔드포인트에 빠진 것:

| 요소 | A2A 1.0 요구사항 | 초안 |
|---|---|---|
| 전송 규격 | JSON-RPC 2.0 바인딩은 `jsonrpc`, `id`, `method`, `params` 필드를 갖춤 | ❌ 평범한 REST body |
| 메서드명 | 핵심 연산은 `SendMessage`, `GetTask`, `ListTasks`, `CancelTask` 등으로 정의됨 | ❌ 임의의 `negotiation/open` |
| 데이터 모델 | `Task`(id, contextId, status, artifacts, history), `Message`(messageId, role, parts), `Part`(text/raw/url/data 중 하나) | ❌ 없음 |
| Agent Card | `capabilities`, `skills`, `securitySchemes`, `security`, 지원 인터페이스 선언 필요 | ❌ 임의 JSON |
| 인증 | `securitySchemes`에 APIKey/HTTPAuth/OAuth2/OIDC/mTLS 중 선언, 서버는 자격증명 없는 요청을 거부해야 함 | ❌ 없음 |
| 버전 협상 | 클라이언트는 `A2A-Version` 헤더를 보내야 함 | ❌ 없음 |
| 전송 보안 | 표준 웹 보안 관행 준수 (HTTPS) | ❌ 평문 HTTP |
| Task 상태 | `TASK_STATE_SUBMITTED` / `WORKING` / `INPUT_REQUIRED` / `COMPLETED` / `FAILED` / `CANCELED` / `REJECTED` / `AUTH_REQUIRED` | ❌ 자체 상태 |

출처: [A2A 1.0 공식 명세](https://a2a-protocol.org/latest/specification/)

## 6.2 MVP에서 쓸 표현

> "FairMeet의 에이전트 간 통신은 **A2A에서 영감을 받은 자체 협상 프로토콜(A2A-inspired custom negotiation protocol)** 입니다. Agent Card를 통한 발견, JSON-RPC 스타일 메시지, 거부/역제안 수행문 등 A2A의 설계 개념을 차용했으나, A2A 1.0 규격의 Task 생명주기·보안 스킴·버전 협상은 구현하지 않았으므로 **A2A 호환이라고 주장하지 않습니다.**"

## 6.3 실제 A2A 1.0 전환 계획 (발표용 로드맵)

| 단계 | 작업 |
|---|---|
| 1 | `a2a-python` SDK 도입, Agent Card를 `/.well-known/agent-card.json`에 규격대로 게시 |
| 2 | 협상 수행문을 `Message` + `Part(data=...)` 구조로 재작성, `SendMessage`로 전송 |
| 3 | 협상 세션을 `Task`로 모델링. `contextId`로 그룹 묶기. `TASK_STATE_INPUT_REQUIRED`를 인간 승인 대기에 매핑 |
| 4 | `securitySchemes`에 OAuth2 또는 HTTPAuth(Bearer) 선언, HTTPS 적용 |
| 5 | Agent Card 서명 적용 (A2A는 서명된 Agent Card로 암호학적 검증 지원) |

**참고할 만한 점:** A2A의 `TASK_STATE_INPUT_REQUIRED`는 인간 개입이 필요한 중단 상태로 정의되어 있어, FairMeet의 `PENDING_HUMAN_APPROVAL` 상태와 의미상 정확히 대응합니다. 전환 시 자연스럽게 매핑됩니다.

---

# 7. 수정된 프로토콜 메시지 예시

## 7.1 Agent Card (A2A-inspired, 규격 미준수임을 명시)

```json
{
  "protocol": "fairmeet-negotiation/0.1",
  "a2aInspired": true,
  "a2aCompliant": false,
  "name": "FairMeet Personal Agent",
  "agentId": "grp7a3f_b81c29d4e5f6",
  "skills": [
    {
      "id": "schedule.negotiate",
      "name": "Schedule negotiation",
      "description": "개인 제약을 공개하지 않고 슬롯별 비용을 산출하여 협상에 참여"
    }
  ],
  "capabilities": { "streaming": false, "counterProposal": true },
  "security": { "scheme": "Bearer" }
}
```

## 7.2 비용 벡터 응답 — **허용 필드 스키마**

```json
{
  "protocol": "fairmeet-negotiation/0.1",
  "method": "negotiation/costs",
  "sessionId": "8f2a...",
  "agentId": "grp7a3f_b81c29d4e5f6",
  "nonce": "6d1e9a2c4b7f0e83",
  "issuedAt": "2026-09-19T03:00:00Z",
  "costs": [12.0, 34.5, null, 0.0, 78.0],
  "vetoMask": [false, false, true, false, false],
  "scaleVersion": "cost-rules-v1"
}
```

**설계 규칙 (테스트로 강제):**
- `costs`의 원소는 `float` 또는 `null`(거부권)만 허용. `null`이면 `vetoMask[i]`가 `true`여야 함
- 최상위 키는 위 9개로 **고정**. 추가 키가 있으면 Mediator가 400으로 거부
- **문자열 값을 갖는 필드는 `sessionId`/`agentId`/`nonce`/`issuedAt`/`protocol`/`method`/`scaleVersion` 뿐**이며, 모두 정규식으로 형식을 검증 → 자유 텍스트가 스며들 수 없음
- `scaleVersion`은 모든 에이전트가 동일한 비용 규칙을 썼는지 검증. 불일치 시 협상 중단

## 7.3 제안 / 응답 — `counter_indices`를 실제로 사용

```json
// Mediator → Agent
{
  "method": "negotiation/propose",
  "sessionId": "8f2a...",
  "round": 1,
  "slotIndex": 17
}

// Agent → Mediator
{
  "method": "negotiation/respond",
  "sessionId": "8f2a...",
  "agentId": "grp7a3f_b81c29d4e5f6",
  "round": 1,
  "verdict": "counter",
  "counterIndices": [22, 23, 29]
}
```

`verdict`는 `accept` | `counter` | `reject` 세 값만 허용합니다. `counterIndices`는 **최대 5개**로 제한하며(더 많으면 비용 벡터를 역추론할 수 있음), Mediator는 다음 라운드에서 이 인덱스들을 **우선 후보 집합**으로 사용합니다(§8.4).

---

# 8. 수정된 공정성 알고리즘과 수치 예시

## 8.1 용어와 부호 정의 — 여기를 틀리면 전부 틀립니다

**`concession_balance_i` (양보 잔액)**

> **양수 = 과거에 더 많이 희생했으므로 시스템이 배려해야 하는 상태**
> **음수 = 과거에 더 유리한 결과를 받았으므로 이번엔 양보할 차례**
> **0 = 균형 상태**

`debt`(빚)라는 단어는 "양수 = 이 사람이 갚아야 함"으로 읽히기 쉬워 의미가 뒤집힙니다. 코드·문서·발표에서 **`concession_balance`로 통일**하세요.

## 8.2 목적함수

```text
# 1단계: regret — 최초 비용 행렬에서 한 번만 계산 (이후 고정)
min_feasible_cost_i = min{ cost_i(s) : s ∈ 전체 슬롯, veto_i(s) = false }
regret_i(s)         = cost_i(s) - min_feasible_cost_i          # 항상 ≥ 0

# 2단계: 잔액 보정
adjusted_i(s) = regret_i(s) + lambda * concession_balance_i

# 3단계: 사전식 최소화 (거부권이 있는 슬롯은 후보에서 제외)
selected_slot = lexicographic_min over s ∈ candidate_mask of (
    max_i adjusted_i(s),      # 1순위: 가장 불리한 사람의 보정 후 불이익을 최소화
    sum_i regret_i(s)         # 2순위: 동점이면 전체 불이익 합이 작은 쪽
)
```

**왜 잔액을 더하는가:** `concession_balance_i`는 슬롯 s에 대해 상수이므로, 잔액이 큰 사용자의 `adjusted` 곡선 전체가 위로 이동합니다. 그러면 `max_i`를 최소화하는 과정에서 **그 사용자의 regret이 낮은 슬롯이 선택될 가능성이 높아집니다.** 부호가 반대(`- lambda * balance`)면 정반대로 작동하니 반드시 확인하세요.

**왜 합이 아니라 최댓값인가:** 합계 최소화는 한 명이 크게 희생하고 나머지가 편한 해를 고릅니다. 최댓값 최소화는 가장 불리한 사람의 불이익을 줄입니다.

## 8.3 수치 예시로 증명

### 예시 1 — 2인, 잔액이 결과를 뒤집는 경우

원본 비용 (λ = 1.0):

| | s1 | s2 | s3 |
|---|---|---|---|
| A | 40 | 10 | veto |
| B | 10 | 35 | 20 |

`min_feasible_cost_A = 10`, `min_feasible_cost_B = 10` → regret:

| | s1 | s2 | s3 |
|---|---|---|---|
| A | 30 | 0 | ∞ |
| B | 0 | 25 | 10 |

**잔액이 모두 0일 때:**

| 슬롯 | max regret | sum regret | 판정 |
|---|---|---|---|
| s1 | 30 | 30 | |
| s2 | 25 | 25 | **선택** (max 25 최소) |
| s3 | — | — | A 거부권으로 제외 |

→ s2 선택. B가 regret 25를 부담. 갱신: mean = (0+25)/2 = 12.5
`balance_A = 0 - 12.5 = -12.5`, `balance_B = 25 - 12.5 = +12.5`

**다음 라운드에서 같은 비용 구조가 나오면:**

| 슬롯 | adjusted_A | adjusted_B | max | 판정 |
|---|---|---|---|---|
| s1 | 30 + (-12.5) = 17.5 | 0 + 12.5 = 12.5 | **17.5** | **선택** |
| s2 | 0 + (-12.5) = -12.5 | 25 + 12.5 = 37.5 | 37.5 | |

→ **s1으로 뒤집혔습니다.** 이번엔 A가 부담합니다. 잔액 갱신: mean = (30+0)/2 = 15
`balance_A = -12.5·decay + (30-15) ≈ +3.9`, `balance_B = 12.5·decay + (0-15) ≈ -3.9`

두 라운드에 걸쳐 잔액이 0 근처로 수렴하며, 양보가 번갈아 발생합니다. 이것이 알고리즘의 핵심 동작입니다.

### 예시 2 — 3인, 잔액 없이는 같은 사람이 계속 손해

regret (λ = 1.0):

| | s1 | s2 |
|---|---|---|
| A | 30 | 5 |
| B | 5 | 32 |
| C | 5 | 5 |

**잔액 0:** s1은 (max 30, sum 40), s2는 (max 32, sum 42) → **s1 선택, A가 부담.**
비용 구조가 매주 비슷하면 A는 **매번** 손해를 봅니다. 이것이 단순 minimax의 한계입니다.

**A의 잔액이 +20으로 누적된 뒤 (B=0, C=-20):**

| 슬롯 | adj_A | adj_B | adj_C | max | sum regret |
|---|---|---|---|---|---|
| s1 | 30+20=50 | 5+0=5 | 5-20=-15 | **50** | 40 |
| s2 | 5+20=25 | 32+0=32 | 5-20=-15 | **32** | 42 |

→ **s2 선택.** A가 해방되고 B가 부담합니다. 잔액이 없었다면 s1이 계속 선택됐을 것입니다.

## 8.4 라운드 진행 — `counter_indices` 사용 방식

```text
Round 0: 전원에게 비용 벡터 요청 → regret 행렬 계산 (이후 고정)
         candidate_mask = 거부권 없는 모든 슬롯
         preferred_set  = ∅

Round r (r = 1..MAX_ROUNDS):
  1. 탐색 대상 = preferred_set ∩ candidate_mask  (비어 있으면 candidate_mask 전체)
  2. solve() → slot*
  3. 전원에게 propose(slot*)
  4. 전원 accept → PENDING_HUMAN_APPROVAL로 전이, 종료
  5. 아니면:
       candidate_mask -= {slot*}
       preferred_set  = ∪(모든 응답의 counterIndices) ∩ candidate_mask
       (preferred_set이 비면 다음 라운드는 전체 mask 탐색)
  6. candidate_mask가 비면 → NO_FEASIBLE_SLOT
```

역제안이 **실제로 다음 탐색 공간을 좁히는 데** 쓰입니다. 이것이 "협상"이라고 부를 수 있는 최소 조건입니다.

## 8.5 잔액 관리 규칙

- **decay:** 반감기 6라운드 → `decay = 0.5 ** (1/6) ≈ 0.891`. 오래된 부담은 서서히 잊힘
- **clamp:** `[-40, +40]`. 극단값이 목적함수를 지배해 거부권처럼 작동하는 것을 방지
- **zero-sum:** `regret - mean(regret)`으로 갱신하므로 잔액 합은 decay를 제외하면 항상 0
- **갱신 시점:** **인간 승인 + Calendar commit 성공 이후에만.** 합의만으로는 갱신하지 않음

## 8.6 전략적 행동의 한계 — 반드시 인정할 것

| 공격 | 효과 | 완화 | 완전 해결? |
|---|---|---|---|
| 모든 슬롯을 HARD로 표시 | 협상 자체가 실패 → `NO_FEASIBLE_SLOT` | 인간에게 에스컬레이션. 자기 목적 달성 못 함 | 부분적 |
| 비용을 일괄 부풀림 | regret은 자기 최솟값 기준 **상대값**이므로 일괄 이동은 효과 없음 | 자동 무효화 | **✅ 구조적으로 방어됨** |
| 특정 슬롯만 비용 과장 | 그 슬롯을 회피시킬 수 있음 | 비용 규칙(`scaleVersion`)과 범위 검증. 완전 방어 불가 | ❌ |
| 거부권 남발 | 협상 공간 축소 | 쿼터제 필요 (§8.7) | 구현 여부에 따름 |

**regret 정규화가 일괄 부풀림을 자동으로 무효화한다는 점은 실제 강점입니다.** 발표에서 언급할 가치가 있습니다. 반대로 특정 슬롯만 과장하는 공격은 막지 못하며, 이 한계를 먼저 말하는 편이 심사에서 유리합니다.

## 8.7 보호시간 쿼터 — 구현하거나 주장을 삭제하거나

주장하려면 이 검증을 반드시 구현하세요:

```python
WEEKLY_PROTECTED_QUOTA_MIN = 600  # 주 10시간

def validate_profile(profile, slots) -> list[str]:
    """프로필 로드 시 1회 검증. 위반 시 협상 참여 거부."""
    errors = []
    total = 0
    for wd, t0, t1 in profile.protected_windows:
        h0, m0 = map(int, t0.split(":"))
        h1, m1 = map(int, t1.split(":"))
        minutes = (h1 * 60 + m1) - (h0 * 60 + m0)
        if minutes <= 0:
            minutes += 24 * 60          # 자정을 넘는 구간
        total += minutes
    if total > WEEKLY_PROTECTED_QUOTA_MIN:
        errors.append(
            f"보호시간 {total}분이 주간 쿼터 {WEEKLY_PROTECTED_QUOTA_MIN}분을 초과합니다"
        )
    return errors
```

**주의:** 이 쿼터는 사용자가 **직접 선언한** 보호 시간대에만 적용됩니다. HARD 캘린더(돌봄·통원 등 실제 일정)에서 유래한 거부권에는 쿼터를 걸 수 없습니다 — 실제 일정을 "너무 많다"는 이유로 무시할 수는 없기 때문입니다. 발표에서 이 구분을 분명히 하세요.

## 8.7.1 동작하는 solver 전체 코드

**파일: `common/fairness.py`**

```python
"""공정성 solver. 외부 의존성 없음 — 단독으로 테스트 가능."""
from __future__ import annotations

import math
from dataclasses import dataclass

DECAY_HALF_LIFE_ROUNDS = 6
BALANCE_CLAMP = 40.0
DEFAULT_LAMBDA = 1.0


@dataclass(frozen=True)
class SolveResult:
    slot_index: int
    regrets: dict[str, float]          # agent_id -> regret (승인 후 잔액 갱신에 사용)
    max_adjusted: float
    sum_regret: float


def build_regret_matrix(
    costs: dict[str, list[float | None]],
) -> tuple[dict[str, list[float | None]], dict[str, float]]:
    """최초 비용 행렬에서 regret을 한 번만 계산한다.

    None = 거부권. 반환된 regret 행렬은 협상 내내 변경하지 않으며,
    라운드마다 바뀌는 것은 candidate_mask 뿐이다.

    Returns:
        (regret 행렬, agent별 min_feasible_cost)
    """
    regret: dict[str, list[float | None]] = {}
    baselines: dict[str, float] = {}

    for agent, vec in costs.items():
        feasible = [c for c in vec if c is not None]
        if not feasible:
            raise ValueError(f"agent {agent} has no feasible slot at all")
        base = min(feasible)
        baselines[agent] = base
        regret[agent] = [None if c is None else float(c) - base for c in vec]

    return regret, baselines


def solve(
    regret: dict[str, list[float | None]],
    balance: dict[str, float],
    candidate_mask: set[int],
    lam: float = DEFAULT_LAMBDA,
) -> SolveResult | None:
    """사전식 최소화: (max_i adjusted_i, sum_i regret_i)

    Args:
        regret: build_regret_matrix가 만든 고정 행렬
        balance: agent_id -> concession_balance (양수 = 배려 대상)
        candidate_mask: 이번 라운드에 검토할 슬롯 인덱스 집합
        lam: 잔액 가중치

    Returns:
        SolveResult, 또는 가능한 슬롯이 없으면 None
    """
    agents = sorted(regret)
    if not agents:
        return None

    best: SolveResult | None = None
    best_key: tuple[float, float] | None = None

    for s in sorted(candidate_mask):
        row = {a: regret[a][s] for a in agents}
        if any(v is None for v in row.values()):
            continue                                    # 거부권 → 절대 불가

        adjusted = {a: row[a] + lam * balance.get(a, 0.0) for a in agents}
        key = (max(adjusted.values()), sum(row.values()))

        if best_key is None or key < best_key:
            best_key = key
            best = SolveResult(
                slot_index=s,
                regrets={a: float(row[a]) for a in agents},
                max_adjusted=key[0],
                sum_regret=key[1],
            )

    return best


def update_balance(
    balance: dict[str, float],
    regrets: dict[str, float],
    half_life: int = DECAY_HALF_LIFE_ROUNDS,
    clamp: float = BALANCE_CLAMP,
) -> dict[str, float]:
    """확정된 슬롯의 regret으로 잔액을 갱신한다.

    반드시 인간 승인 + Calendar commit 성공 이후에만 호출할 것.
    편차 기반이므로 decay를 제외하면 잔액 합은 0으로 유지된다.
    """
    if not regrets:
        return dict(balance)

    decay = 0.5 ** (1.0 / half_life)
    mean_regret = sum(regrets.values()) / len(regrets)

    out = dict(balance)
    for agent, r in regrets.items():
        prev = out.get(agent, 0.0)
        nxt = prev * decay + (r - mean_regret)
        out[agent] = max(-clamp, min(clamp, nxt))
    return out


# ---- 베이스라인 전략 (평가용) -------------------------------------------

def solve_first_fit(regret, balance, candidate_mask, lam=0.0) -> SolveResult | None:
    """가장 이른 가능 슬롯. '빠른 사람이 먼저 잡는' 현실의 기본값."""
    agents = sorted(regret)
    for s in sorted(candidate_mask):
        row = {a: regret[a][s] for a in agents}
        if any(v is None for v in row.values()):
            continue
        return SolveResult(s, {a: float(row[a]) for a in agents},
                           max(row.values()), sum(row.values()))
    return None


def solve_sum_min(regret, balance, candidate_mask, lam=0.0) -> SolveResult | None:
    """총합 최소화. 효율은 좋지만 한 명이 크게 희생할 수 있다."""
    agents = sorted(regret)
    best, best_key = None, None
    for s in sorted(candidate_mask):
        row = {a: regret[a][s] for a in agents}
        if any(v is None for v in row.values()):
            continue
        key = (sum(row.values()), max(row.values()))
        if best_key is None or key < best_key:
            best_key = key
            best = SolveResult(s, {a: float(row[a]) for a in agents},
                               max(row.values()), sum(row.values()))
    return best


def solve_minimax(regret, balance, candidate_mask, lam=0.0) -> SolveResult | None:
    """잔액 없는 순수 minimax. FairMeet에서 잔액만 뺀 버전."""
    return solve(regret, {a: 0.0 for a in regret}, candidate_mask, lam=0.0)


def gini(values: list[float]) -> float:
    """누적 부담의 불평등 지수. 0 = 완전 평등."""
    xs = sorted(v for v in values)
    n = len(xs)
    if n == 0:
        return 0.0
    shift = min(0.0, min(xs))
    xs = [x - shift for x in xs]        # 음수 방지
    total = sum(xs)
    if total == 0:
        return 0.0
    cum = sum((i + 1) * x for i, x in enumerate(xs))
    return (2.0 * cum) / (n * total) - (n + 1.0) / n
```

**단위 테스트 (`tests/test_fairness.py`):**

```python
import pytest
from common.fairness import (
    build_regret_matrix, solve, update_balance, solve_sum_min, solve_minimax,
)


def test_regret_is_relative_to_own_minimum():
    costs = {"A": [40.0, 10.0, None], "B": [10.0, 35.0, 20.0]}
    regret, base = build_regret_matrix(costs)
    assert base == {"A": 10.0, "B": 10.0}
    assert regret["A"] == [30.0, 0.0, None]
    assert regret["B"] == [0.0, 25.0, 10.0]


def test_uniform_cost_inflation_has_no_effect():
    """일괄 부풀림 공격이 regret 정규화로 무효화되는지."""
    honest = {"A": [40.0, 10.0, 60.0], "B": [10.0, 35.0, 20.0]}
    inflated = {"A": [90.0, 60.0, 110.0], "B": [10.0, 35.0, 20.0]}
    r1, _ = build_regret_matrix(honest)
    r2, _ = build_regret_matrix(inflated)
    assert r1 == r2


def test_balance_flips_the_decision():
    """예시 1: 잔액이 쌓이면 다음 라운드에 결과가 뒤집힌다."""
    costs = {"A": [40.0, 10.0, None], "B": [10.0, 35.0, 20.0]}
    regret, _ = build_regret_matrix(costs)
    mask = {0, 1, 2}

    r0 = solve(regret, {"A": 0.0, "B": 0.0}, mask)
    assert r0.slot_index == 1                       # s2 선택, B가 부담

    bal = update_balance({}, r0.regrets)
    assert bal["A"] < 0 and bal["B"] > 0            # 부호 확인

    r1 = solve(regret, bal, mask)
    assert r1.slot_index == 0                       # s1으로 뒤집힘, A가 부담


def test_veto_slot_is_never_selected():
    costs = {"A": [None, 90.0], "B": [0.0, 90.0]}
    regret, _ = build_regret_matrix(costs)
    res = solve(regret, {}, {0, 1})
    assert res.slot_index == 1


def test_no_feasible_slot_returns_none():
    costs = {"A": [None, 5.0], "B": [5.0, None]}
    regret, _ = build_regret_matrix(costs)
    assert solve(regret, {}, {0, 1}) is None


def test_balance_is_zero_sum_modulo_decay():
    bal = update_balance({}, {"A": 30.0, "B": 0.0, "C": 15.0})
    assert abs(sum(bal.values())) < 1e-9


def test_balance_is_clamped():
    bal = {}
    for _ in range(50):
        bal = update_balance(bal, {"A": 100.0, "B": 0.0})
    assert abs(bal["A"]) <= 40.0 + 1e-9


def test_minimax_differs_from_sum_min():
    """예시 2: 합계 최소화와 minimax가 다른 답을 내는지."""
    costs = {"A": [30.0, 5.0], "B": [5.0, 32.0], "C": [5.0, 5.0]}
    regret, _ = build_regret_matrix(costs)
    assert solve_minimax(regret, {}, {0, 1}).slot_index == 0
    assert solve_sum_min(regret, {}, {0, 1}).slot_index == 0
    # 잔액이 쌓이면 FairMeet만 답이 달라진다
    bal = {"A": 20.0, "B": 0.0, "C": -20.0}
    assert solve(regret, bal, {0, 1}).slot_index == 1
```

---

# 9. 협상 및 승인 상태 머신

## 9.1 상태 전이도

```text
                    ┌──────┐
                    │ OPEN │  세션 생성, 슬롯 그리드 확정
                    └──┬───┘
                       │ 전원 비용 벡터 수신
                       ▼
              ┌──────────────────┐
              │ COSTS_COLLECTED  │  regret 행렬 계산 (이후 고정)
              └────────┬─────────┘
                       │
                       ▼
              ┌──────────────────┐   라운드 소진    ┌───────────────────┐
              │ AGENT_NEGOTIATING├─────────────────▶│ NO_FEASIBLE_SLOT  │
              └────────┬─────────┘   후보 소진      └───────────────────┘
                       │ 전원 accept
                       ▼
        ┌────────────────────────────┐  1명이라도 거절  ┌──────────┐
        │  PENDING_HUMAN_APPROVAL    ├─────────────────▶│ REJECTED │
        │  (참가자별 개별 DM/링크)    │                  └──────────┘
        └────────────┬───────────────┘  TTL 초과        ┌──────────┐
                     │ 전원 승인       ────────────────▶│ EXPIRED  │
                     ▼
        ┌────────────────────────────┐   충돌 발견      ┌────────────────┐
        │   RECHECKING_CALENDARS     ├─────────────────▶│ STALE_CONFLICT │
        │   (전원 FreeBusy 재조회)    │                  └───────┬────────┘
        └────────────┬───────────────┘                          │ 슬롯 제외 후
                     │ 충돌 없음                                 │ 재협상
                     ▼                                          ▼
              ┌──────────────┐    insert 실패    ┌──────────────┐
              │  COMMITTING  ├──────────────────▶│ COMMIT_FAILED│
              └──────┬───────┘                   └──────────────┘
                     │ insert 성공 (또는 409 = 이미 존재)
                     ▼
              ┌──────────────┐
              │  COMMITTED   │  ← 여기서만 concession_balance 갱신
              └──────────────┘

  (임의 시점) 에이전트 응답 없음 → AGENT_UNAVAILABLE
```

## 9.2 상태별 불변식

| 상태 | 불변식 |
|---|---|
| `COSTS_COLLECTED` | regret 행렬이 DB에 저장되어 있고 이후 절대 변경되지 않음 |
| `AGENT_NEGOTIATING` | `candidate_mask`는 단조 감소만 함 (슬롯이 다시 추가되지 않음) |
| `PENDING_HUMAN_APPROVAL` | `expires_at`이 설정됨. 제안 슬롯이 고정됨 |
| `RECHECKING_CALENDARS` | 승인 레코드가 참가자 수와 일치 |
| `COMMITTING` | `calendar_event_id`가 결정론적으로 계산되어 있음 |
| `COMMITTED` | `google_event_id`가 기록됨. 잔액이 정확히 1회 갱신됨 (`balance_applied = 1`) |

**핵심 규칙:** 모든 상태 전이는 **단일 SQL 트랜잭션** 안에서 `WHERE state = <예상 이전 상태>` 조건과 함께 수행합니다. 갱신 행 수가 0이면 다른 요청이 이미 전이시킨 것이므로 조용히 종료합니다. 이것이 중복 클릭·재전송에 대한 멱등성의 근거입니다.

---

# 10. DB 스키마

**파일: `mediator/schema.sql`**

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 협상 세션
CREATE TABLE IF NOT EXISTS sessions (
    session_id        TEXT PRIMARY KEY,
    group_id          TEXT NOT NULL,
    mode              TEXT NOT NULL CHECK (mode IN ('work', 'social')),
    duration_min      INTEGER NOT NULL,
    state             TEXT NOT NULL,
    slots_json        TEXT NOT NULL,   -- [[iso_start, iso_end], ...] 인덱스 = 슬롯 번호
    regret_json       TEXT,            -- {agent_id: [r|null, ...]}  ← 계산 후 불변
    candidate_mask    TEXT,            -- JSON 정수 배열
    preferred_set     TEXT,            -- JSON 정수 배열 (역제안 누적)
    round             INTEGER NOT NULL DEFAULT 0,
    proposed_slot     INTEGER,
    proposed_regrets  TEXT,            -- {agent_id: regret}
    organizer_agent   TEXT,
    google_event_id   TEXT,
    balance_applied   INTEGER NOT NULL DEFAULT 0,  -- 0/1, 잔액 중복 갱신 방지
    fail_reason       TEXT,
    expires_at        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);
CREATE INDEX IF NOT EXISTS idx_sessions_group ON sessions(group_id);

-- 참가자 (세션 단위 스냅샷)
CREATE TABLE IF NOT EXISTS participants (
    session_id     TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id       TEXT NOT NULL,        -- HMAC 가명
    agent_url      TEXT NOT NULL,
    display_name   TEXT NOT NULL,        -- 승인 화면 표시용
    email          TEXT NOT NULL,        -- attendee 초대용
    is_organizer   INTEGER NOT NULL DEFAULT 0,
    approval_token TEXT NOT NULL,        -- 1회용, 개인 승인 링크에 사용
    PRIMARY KEY (session_id, agent_id)
);

-- 개별 승인 (공개 채널에 노출 금지)
CREATE TABLE IF NOT EXISTS approvals (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id    TEXT NOT NULL,
    verdict     TEXT NOT NULL CHECK (verdict IN ('approve', 'reject')),
    decided_at  TEXT NOT NULL,
    PRIMARY KEY (session_id, agent_id)   -- 중복 클릭 자동 무시
);

-- 양보 잔액 (그룹별로 분리 — 회사 잔액과 친구 잔액은 별개)
CREATE TABLE IF NOT EXISTS ledger (
    group_id           TEXT NOT NULL,
    agent_id           TEXT NOT NULL,
    concession_balance REAL NOT NULL DEFAULT 0.0,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (group_id, agent_id)
);

-- 이력 (평가 지표 산출용)
CREATE TABLE IF NOT EXISTS history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id       TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    agent_id       TEXT NOT NULL,
    slot_start     TEXT NOT NULL,
    regret         REAL NOT NULL,
    balance_after  REAL NOT NULL,
    committed_at   TEXT NOT NULL
);

-- 감사 로그 (상태 전이 추적)
CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    from_state  TEXT,
    to_state    TEXT NOT NULL,
    detail      TEXT,
    at          TEXT NOT NULL
);

-- replay 방지
CREATE TABLE IF NOT EXISTS seen_nonces (
    nonce   TEXT PRIMARY KEY,
    seen_at TEXT NOT NULL
);
```

## 10.1 상태 저장과 전이 — 전체 코드

**파일: `mediator/store.py`**

```python
"""협상 세션의 영속화와 상태 전이. 모든 전이는 조건부 UPDATE로 멱등."""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import datetime as dt
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("FAIRMEET_DB", "fairmeet.db"))
SCHEMA = Path(__file__).with_name("schema.sql")

TERMINAL = {"COMMITTED", "REJECTED", "EXPIRED", "NO_FEASIBLE_SLOT",
            "AGENT_UNAVAILABLE", "COMMIT_FAILED"}


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def init_db() -> None:
    with connect() as c:
        c.executescript(SCHEMA.read_text(encoding="utf-8"))


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---- 생성 ---------------------------------------------------------------

def create_session(session_id, group_id, mode, duration_min, slots, participants):
    """participants: [{agent_id, agent_url, display_name, email, is_organizer}]"""
    ts = now()
    slots_json = json.dumps([[s.isoformat(), e.isoformat()] for s, e in slots])
    with connect() as c:
        c.execute(
            """INSERT INTO sessions
               (session_id, group_id, mode, duration_min, state, slots_json,
                round, balance_applied, created_at, updated_at)
               VALUES (?,?,?,?,'OPEN',?,0,0,?,?)""",
            (session_id, group_id, mode, duration_min, slots_json, ts, ts),
        )
        for p in participants:
            c.execute(
                """INSERT INTO participants
                   (session_id, agent_id, agent_url, display_name, email,
                    is_organizer, approval_token)
                   VALUES (?,?,?,?,?,?,?)""",
                (session_id, p["agent_id"], p["agent_url"], p["display_name"],
                 p["email"], int(p.get("is_organizer", 0)),
                 secrets.token_urlsafe(24)),
            )
        c.execute("INSERT INTO transitions (session_id, from_state, to_state, at) "
                  "VALUES (?, NULL, 'OPEN', ?)", (session_id, ts))


# ---- 조회 ---------------------------------------------------------------

def get_session(session_id) -> sqlite3.Row | None:
    with connect() as c:
        return c.execute("SELECT * FROM sessions WHERE session_id = ?",
                         (session_id,)).fetchone()


def get_participants(session_id) -> list[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM participants WHERE session_id = ? "
                         "ORDER BY agent_id", (session_id,)).fetchall()


def get_balance(group_id, agent_ids) -> dict[str, float]:
    with connect() as c:
        rows = c.execute(
            "SELECT agent_id, concession_balance FROM ledger WHERE group_id = ?",
            (group_id,)).fetchall()
    known = {r["agent_id"]: r["concession_balance"] for r in rows}
    return {a: known.get(a, 0.0) for a in agent_ids}


# ---- 전이 ---------------------------------------------------------------

def transition(session_id, expect_state, to_state, **fields) -> bool:
    """조건부 상태 전이. 이미 다른 상태면 False를 반환하고 아무것도 하지 않는다.

    이것이 중복 요청·Slack 재전송·동시 클릭에 대한 멱등성의 근거다.
    """
    ts = now()
    sets = ["state = ?", "updated_at = ?"]
    vals: list = [to_state, ts]
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        vals.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
    vals += [session_id, expect_state]

    with connect() as c:
        cur = c.execute(
            f"UPDATE sessions SET {', '.join(sets)} "
            f"WHERE session_id = ? AND state = ?", vals)
        if cur.rowcount == 0:
            return False
        c.execute("INSERT INTO transitions (session_id, from_state, to_state, at) "
                  "VALUES (?,?,?,?)", (session_id, expect_state, to_state, ts))
    return True


def save_regret_matrix(session_id, regret, candidate_mask) -> bool:
    return transition(
        session_id, "OPEN", "COSTS_COLLECTED",
        regret_json=json.dumps(regret),
        candidate_mask=json.dumps(sorted(candidate_mask)),
        preferred_set=json.dumps([]),
    )


def load_regret(session_id):
    """저장된 regret 행렬을 그대로 반환한다. 절대 재계산하지 않는다."""
    row = get_session(session_id)
    if not row or not row["regret_json"]:
        return None, set(), set()
    return (json.loads(row["regret_json"]),
            set(json.loads(row["candidate_mask"] or "[]")),
            set(json.loads(row["preferred_set"] or "[]")))


def advance_round(session_id, candidate_mask, preferred_set, round_no) -> None:
    with connect() as c:
        c.execute(
            """UPDATE sessions SET candidate_mask = ?, preferred_set = ?,
               round = ?, updated_at = ? WHERE session_id = ?""",
            (json.dumps(sorted(candidate_mask)), json.dumps(sorted(preferred_set)),
             round_no, now(), session_id))


# ---- 승인 ---------------------------------------------------------------

def record_approval(session_id, agent_id, verdict) -> str:
    """개별 승인 기록. 반환: 'recorded' | 'duplicate' | 'not_pending'

    PRIMARY KEY 제약이 중복 클릭을 자동으로 무시한다.
    """
    with connect() as c:
        row = c.execute("SELECT state, expires_at FROM sessions WHERE session_id = ?",
                        (session_id,)).fetchone()
        if not row or row["state"] != "PENDING_HUMAN_APPROVAL":
            return "not_pending"
        if row["expires_at"] and row["expires_at"] < now():
            return "not_pending"
        try:
            c.execute("INSERT INTO approvals (session_id, agent_id, verdict, decided_at) "
                      "VALUES (?,?,?,?)", (session_id, agent_id, verdict, now()))
        except sqlite3.IntegrityError:
            return "duplicate"
    return "recorded"


def approval_tally(session_id) -> tuple[int, int, int]:
    """(승인 수, 거절 수, 전체 참가자 수)"""
    with connect() as c:
        a = c.execute("SELECT verdict, COUNT(*) n FROM approvals "
                      "WHERE session_id = ? GROUP BY verdict",
                      (session_id,)).fetchall()
        total = c.execute("SELECT COUNT(*) n FROM participants WHERE session_id = ?",
                          (session_id,)).fetchone()["n"]
    counts = {r["verdict"]: r["n"] for r in a}
    return counts.get("approve", 0), counts.get("reject", 0), total


def expire_stale_sessions() -> list[str]:
    """TTL이 지난 승인 대기 세션을 EXPIRED로 전이. 주기적으로 호출."""
    ts = now()
    with connect() as c:
        rows = c.execute(
            "SELECT session_id FROM sessions "
            "WHERE state = 'PENDING_HUMAN_APPROVAL' AND expires_at < ?",
            (ts,)).fetchall()
        ids = [r["session_id"] for r in rows]
        for sid in ids:
            c.execute("UPDATE sessions SET state='EXPIRED', updated_at=? "
                      "WHERE session_id=? AND state='PENDING_HUMAN_APPROVAL'",
                      (ts, sid))
    return ids


# ---- 잔액 (커밋 성공 후에만) ----------------------------------------------

def apply_balance_once(session_id) -> bool:
    """커밋 성공 후 잔액을 정확히 1회 갱신한다.

    balance_applied 플래그를 같은 트랜잭션에서 확인하고 세팅하므로,
    중복 호출되어도 두 번 적용되지 않는다.
    """
    from common.fairness import update_balance

    with connect() as c:
        row = c.execute(
            "SELECT group_id, proposed_regrets, proposed_slot, slots_json, "
            "       balance_applied, state "
            "FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row or row["state"] != "COMMITTED" or row["balance_applied"]:
            return False

        group_id = row["group_id"]
        regrets = json.loads(row["proposed_regrets"])
        slot_start = json.loads(row["slots_json"])[row["proposed_slot"]][0]

        cur = c.execute("SELECT agent_id, concession_balance FROM ledger "
                        "WHERE group_id = ?", (group_id,)).fetchall()
        balance = {r["agent_id"]: r["concession_balance"] for r in cur}
        for a in regrets:
            balance.setdefault(a, 0.0)

        new_balance = update_balance(balance, regrets)
        ts = now()
        for agent, val in new_balance.items():
            c.execute(
                """INSERT INTO ledger (group_id, agent_id, concession_balance, updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(group_id, agent_id)
                   DO UPDATE SET concession_balance = excluded.concession_balance,
                                 updated_at = excluded.updated_at""",
                (group_id, agent, val, ts))
        for agent, r in regrets.items():
            c.execute(
                """INSERT INTO history
                   (group_id, session_id, agent_id, slot_start, regret,
                    balance_after, committed_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (group_id, session_id, agent, slot_start, r,
                 new_balance[agent], ts))

        c.execute("UPDATE sessions SET balance_applied = 1, updated_at = ? "
                  "WHERE session_id = ?", (ts, session_id))
    return True
```

## 10.2 payload 허용 필드 검증

**파일: `mediator/payload_guard.py`**

```python
"""Mediator가 받는 payload에 자유 텍스트가 섞이지 않는지 강제한다.

'privacy leak = 0' 같은 주장은 하지 않는다. 이 테스트가 보장하는 것은
'제목·설명·위치·원본 캘린더 ID 등 명시된 민감 필드가 전송되지 않았다'는
사실 하나뿐이며, 비용 벡터 자체가 메타데이터라는 한계는 여전히 남는다.
"""
import re

ALLOWED_COST_KEYS = {
    "protocol", "method", "sessionId", "agentId", "nonce",
    "issuedAt", "costs", "vetoMask", "scaleVersion",
}

PATTERNS = {
    "protocol":     re.compile(r"^fairmeet-negotiation/\d+\.\d+$"),
    "method":       re.compile(r"^negotiation/[a-z]+$"),
    "sessionId":    re.compile(r"^[0-9a-f]{32}$"),
    "agentId":      re.compile(r"^[a-z0-9]{4,8}_[0-9a-f]{12}$"),
    "nonce":        re.compile(r"^[0-9a-f]{16,32}$"),
    "issuedAt":     re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+(Z|[+-]\d{2}:\d{2})$"),
    "scaleVersion": re.compile(r"^cost-rules-v\d+$"),
}


class PayloadViolation(ValueError):
    pass


def validate_cost_payload(payload: dict, n_slots: int) -> None:
    extra = set(payload) - ALLOWED_COST_KEYS
    if extra:
        raise PayloadViolation(f"허용되지 않은 필드: {sorted(extra)}")
    missing = ALLOWED_COST_KEYS - set(payload)
    if missing:
        raise PayloadViolation(f"누락된 필드: {sorted(missing)}")

    for key, pat in PATTERNS.items():
        val = payload[key]
        if not isinstance(val, str) or not pat.fullmatch(val):
            raise PayloadViolation(f"{key} 형식 위반: {val!r}")

    costs, veto = payload["costs"], payload["vetoMask"]
    if not isinstance(costs, list) or len(costs) != n_slots:
        raise PayloadViolation(f"costs 길이가 {n_slots}이 아님")
    if not isinstance(veto, list) or len(veto) != n_slots:
        raise PayloadViolation(f"vetoMask 길이가 {n_slots}이 아님")

    for i, (c, v) in enumerate(zip(costs, veto)):
        if not isinstance(v, bool):
            raise PayloadViolation(f"vetoMask[{i}]가 bool이 아님")
        if v:
            if c is not None:
                raise PayloadViolation(f"veto 슬롯 {i}의 cost가 null이 아님")
        else:
            if not isinstance(c, (int, float)) or isinstance(c, bool):
                raise PayloadViolation(f"costs[{i}]가 숫자가 아님")
            if not (0.0 <= float(c) <= 100.0):
                raise PayloadViolation(f"costs[{i}]={c}가 [0,100] 범위 밖")
```

**대응 테스트 (`tests/test_payload_guard.py`):**

```python
import pytest
from mediator.payload_guard import validate_cost_payload, PayloadViolation

BASE = {
    "protocol": "fairmeet-negotiation/0.1",
    "method": "negotiation/costs",
    "sessionId": "a" * 32,
    "agentId": "grp7_0123456789ab",
    "nonce": "0123456789abcdef",
    "issuedAt": "2026-09-19T03:00:00Z",
    "costs": [10.0, None],
    "vetoMask": [False, True],
    "scaleVersion": "cost-rules-v1",
}


def test_valid_payload_passes():
    validate_cost_payload(dict(BASE), n_slots=2)


@pytest.mark.parametrize("bad_field", [
    "eventTitle", "calendarId", "reason", "notes", "location", "attendees",
])
def test_sensitive_fields_are_rejected(bad_field):
    p = dict(BASE)
    p[bad_field] = "치과 진료"
    with pytest.raises(PayloadViolation):
        validate_cost_payload(p, n_slots=2)


def test_veto_must_have_null_cost():
    p = dict(BASE, costs=[10.0, 99.0], vetoMask=[False, True])
    with pytest.raises(PayloadViolation):
        validate_cost_payload(p, n_slots=2)


def test_cost_out_of_range_rejected():
    p = dict(BASE, costs=[500.0, None])
    with pytest.raises(PayloadViolation):
        validate_cost_payload(p, n_slots=2)


def test_scale_version_mismatch_is_detectable():
    p = dict(BASE, scaleVersion="cost-rules-v9")
    validate_cost_payload(p, n_slots=2)   # 형식은 통과
    assert p["scaleVersion"] != "cost-rules-v1"   # 비교는 Mediator가 수행
```

---

# 11. Windows · VS Code · Claude Code 초기 설정

> **이 장은 PowerShell 기준입니다.** macOS/Linux 명령은 §11.9에 따로 모았습니다. Bash 문법과 PowerShell 문법을 섞지 마세요 — `PROFILE_PATH=x uvicorn ...` 같은 Bash 인라인 환경변수는 PowerShell에서 동작하지 않습니다.

## 11.A 전체 준비물과 계정

| 항목 | 필요 이유 | MVP 필수? | 과금 가능성 |
|---|---|---|---|
| **Google 계정 3개** | 시연용 참가자. 개인 Gmail로 충분 | **필수** | 없음 |
| **Google Cloud 프로젝트** | Calendar API 사용, OAuth 클라이언트 발급 | **필수** | Calendar API는 무료 할당량 내 사용. 카드 등록 불필요 |
| **Claude 유료 구독 또는 Console 계정** | Claude Code(VS Code 확장/CLI) 사용 | 개발 도구로 필요 | 구독료. 무료 claude.ai 플랜은 Claude Code 미포함 |
| **Anthropic API 키** | **애플리케이션**이 LLM을 호출할 때 | **선택** (§15 fallback 있음) | 사용량 과금. **Claude Code 구독과 별개** |
| **Slack 워크스페이스** | 승인 UI를 Slack으로 할 경우 | **선택** (웹 승인으로 대체) | 무료 플랜 가능 |
| **ngrok 등 터널** | Slack이 로컬 서버에 접근하려면 | Slack 쓸 때만 | 무료 플랜 가능 |

> **⚠️ 가장 흔한 혼동:** Claude Code에 로그인하는 계정과, FairMeet 애플리케이션이 `ANTHROPIC_API_KEY`로 호출하는 것은 **완전히 별개의 과금 경로**입니다. Claude Code 구독이 있어도 애플리케이션 API 호출은 따로 청구됩니다.

> **가격은 변동됩니다.** 금액을 단정하지 말고 [Anthropic 가격 페이지](https://www.anthropic.com/pricing)와 [Google Cloud 가격](https://cloud.google.com/pricing)에서 직접 확인하세요.

### 과금 한도 설정 (API 키 발급 **전에** 할 것)

1. **Anthropic Console** → Settings → Limits → 월 지출 한도(spend limit)와 사용량 알림 설정
2. **Google Cloud** → 결제 → 예산 및 알림 → 예산 생성 (Calendar API는 무료지만 습관적으로)
3. 팀 공용 키를 만들지 말고 **개인별로 발급**해서 누가 얼마나 썼는지 추적 가능하게 하세요

## 11.B Windows 기본 도구 설치

각 항목마다 **설치 → 확인 명령 → 기대 출력** 순서로 진행하세요.

### 1. VS Code

- 다운로드: https://code.visualstudio.com/download → "Windows x64 User Installer"
- 설치 옵션에서 **"Code로 열기 작업을 Windows 탐색기 파일/디렉터리 상황에 맞는 메뉴에 추가"** 두 개를 모두 체크하고, **"PATH에 추가"** 를 반드시 체크하세요
- ⚠️ Claude Code 확장은 **VS Code 1.94.0 이상**이 필요합니다. Help → About에서 확인

확인 (PowerShell):

```powershell
code --version
```

기대 출력: `1.9x.x` 로 시작하는 3줄

### 2. Git for Windows

- 다운로드: https://git-scm.com/downloads/win
- 설치 중 "Adjusting your PATH environment" 에서 **"Git from the command line and also from 3rd-party software"** 선택
- Claude Code는 Windows 네이티브 환경에서 Git for Windows가 있으면 Git Bash를 Bash 도구로 사용하고, 없으면 PowerShell 도구를 대신 사용합니다. 선택 사항이지만 설치를 권장합니다

```powershell
git --version
```

기대 출력: `git version 2.4x.x.windows.1`

### 3. Python

- 다운로드: https://www.python.org/downloads/windows/ → **Python 3.11 또는 3.12** 권장
  (3.13은 일부 패키지 휠이 아직 없을 수 있어 해커톤에는 위험)
- ⚠️ 설치 첫 화면에서 **"Add python.exe to PATH"** 체크박스를 반드시 체크하세요. 이걸 놓치면 §20의 첫 번째 오류가 발생합니다

```powershell
python --version
pip --version
```

기대 출력: `Python 3.12.x` / `pip 24.x from ...`

> `python`을 입력했는데 Microsoft Store가 열리면 PATH 설정이 잘못된 것입니다. §20 참조.

### 4. Node.js

**FairMeet에는 필요 없습니다.** Claude Code도 네이티브 설치를 쓰면 Node.js가 필요 없습니다. 설치하지 마세요 (설치 항목이 하나 줄어듭니다).

### 5. Claude Code CLI (선택)

VS Code 확장만 쓸 거면 **건너뛰어도 됩니다.** 확장은 자체 CLI 복사본을 번들로 포함하고 있어서 채팅 패널이 독립적으로 동작합니다. 터미널에서 `claude` 명령을 쓰고 싶을 때만 별도 설치가 필요합니다.

PowerShell에서 (관리자 권한 불필요):

```powershell
irm https://claude.ai/install.ps1 | iex
```

확인:

```powershell
claude --version
claude doctor
```

기대 출력: `2.x.x (Claude Code)` / `claude doctor`는 설치 상태 진단을 출력

> `'irm' is not recognized` 오류가 나면 PowerShell이 아니라 CMD에 있는 것입니다. 프롬프트가 `PS C:\`로 시작하는지 확인하세요.
>
> 시스템 요구사항: Windows 10 1809+ 또는 Windows Server 2019+, 4GB 이상 RAM.

## 11.C VS Code에서 Claude Code 사용하기

### 설치

1. VS Code에서 `Ctrl+Shift+X` (Extensions)
2. "Claude Code" 검색
3. **게시자가 `Anthropic` 인지 반드시 확인** (유사 이름의 서드파티 확장이 있습니다)
4. **Install**
5. 확장이 안 보이면 Command Palette(`Ctrl+Shift+P`) → **Developer: Reload Window**

### 로그인

1. 파일을 하나 연 상태에서 에디터 우측 상단의 **Spark 아이콘**을 클릭 (아이콘은 파일이 열려 있어야 나타납니다)
   - 또는 `Ctrl+Shift+P` → "Claude Code: Open in Side Bar"
2. 처음 열면 로그인 화면이 나타납니다. **Sign in** → 브라우저에서 인증
3. **API 키는 필요 없습니다.** 유료 Claude 구독(Pro/Max/Team/Enterprise) 또는 Console 계정으로 로그인합니다

> `Not logged in · Please run /login` 이 계속 뜨면 **Developer: Reload Window** 를 실행하세요.

### 권한 모드 — 처음에는 Plan 또는 Manual

프롬프트 박스 하단의 모드 표시를 클릭해서 전환합니다.

| 모드 | 동작 | 권장 상황 |
|---|---|---|
| **Plan** | 무엇을 할지 설명하고 승인을 기다림. 계획이 Markdown 문서로 열리고 인라인 코멘트를 달 수 있음 | **초기 검토 단계** |
| **Manual** | 파일 수정과 대부분의 셸 명령 전에 매번 허락을 구함. 좌우 비교 diff를 보여줌 | **구현 초반** |
| **Auto** | 분류기가 대부분의 동작을 대신 검토. Pro/Max/Team 플랜의 기본 시작 모드 | 익숙해진 뒤 |
| **Edit automatically** | 묻지 않고 편집 | 권장하지 않음 |

**처음에 Plan/Manual을 쓰는 이유:** FairMeet은 OAuth 토큰과 `.env`를 다루는 프로젝트입니다. 자동 편집 모드에서 AI가 `.gitignore`나 `settings.json`을 건드리면 비밀 파일이 커밋될 위험이 있습니다. 작업 흐름이 안정된 뒤에 Auto로 올리세요.

### 파일 참조 (`@`)

```text
@docs/original-plan.md 를 읽어라
@src/components/ 의 구조를 설명해라   ← 폴더는 뒤에 / 를 붙임
@fair                                  ← 퍼지 매칭 (fairness.py 등을 찾음)
```

- 에디터에서 텍스트를 선택하면 Claude가 자동으로 봅니다
- `Alt+K`로 `@파일#5-10` 형태의 줄 범위 참조를 삽입할 수 있습니다

### diff 검토 흐름

Manual 모드에서 Claude가 파일을 수정하려 하면:

1. 원본과 제안 변경의 **좌우 비교** 가 열립니다
2. **Accept** / **Reject** / 또는 "이렇게 대신 해줘"라고 지시
3. diff 화면에서 **직접 내용을 편집한 뒤 accept** 할 수도 있습니다. 이 경우 Claude는 사용자가 수정했다는 사실을 전달받습니다
4. 되돌리려면 메시지에 마우스를 올려 **rewind** 버튼 → "Rewind code to here"

### 터미널에서 테스트 실행

`` Ctrl+` `` 로 통합 터미널을 열고 `pytest` 등을 실행합니다. 터미널 출력을 Claude에게 보여주려면 `@terminal:이름` 으로 참조하세요.

### 세션 관리

- 패널 상단 **Session history** 버튼으로 이전 대화 재개
- `Ctrl+Shift+Esc` 로 새 탭에서 새 대화 시작
- 여러 대화를 탭으로 병렬 운영 가능

## 11.D Claude Code에 이 프롬프트 전달하기

### 파일 배치

```powershell
# 프로젝트 루트에서 실행
New-Item -ItemType Directory -Force -Path docs
# 기존 기획 초안을 docs\original-plan.md 로 저장
# 이 검토 요청 프롬프트를 docs\implementation-review-request.md 로 저장
# 이 문서를 docs\implementation-blueprint.md 로 저장
```

### 1단계 — 코드 수정 없이 검토만

Plan 모드로 전환한 뒤, 프롬프트 박스에 다음을 그대로 입력합니다:

```text
@docs/original-plan.md 와 @docs/implementation-review-request.md 를 모두 읽어라.
아직 파일을 수정하지 말고 현재 저장소 구조와 요구사항을 분석하라.
P0/P1/P2 문제, 수정 아키텍처, 구현 순서, 검증 계획을 먼저 제시하라.
추측한 내용은 "가정"이라고 표시하고, 공식 API 문서로 확인해야 하는 내용은
"확인 필요"로 별도 표시하라.
결과를 docs/review.md 에 작성하라.
```

### 2단계 — 구현 계획 생성

```text
@docs/review.md 의 P0 항목에 동의한다.
이제 구현 계획을 세워라. 각 단계는 독립적으로 테스트 가능한 체크포인트를
가져야 하고, 다음을 명시하라: 목표 / 선행 조건 / 수정·생성 파일 /
실행 위치 / 실행 명령 / 기대 결과 / 자동 테스트 / 완료 조건.
아직 코드를 작성하지 마라.
```

### 3단계 — 한 단계씩 구현

Manual 모드로 전환한 뒤:

```text
계획의 1단계만 구현하라. 구현 후 pytest를 실행하고 결과를 보고하라.
다음 단계로 넘어가지 마라.
```

**중요:** 한 번에 "전부 구현해줘"라고 하지 마세요. OAuth·상태머신·멱등성이 얽힌 코드는 단계별 검증 없이 작성하면 어디서 깨졌는지 추적이 불가능합니다.

## 11.E Python 프로젝트 초기화

**모든 명령은 프로젝트 루트(`C:\dev\fairmeet`)에서 실행합니다.**

### 폴더 생성과 VS Code 열기

```powershell
New-Item -ItemType Directory -Force -Path C:\dev\fairmeet
Set-Location C:\dev\fairmeet
git init
code .
```

### 가상환경

```powershell
# 위치: C:\dev\fairmeet
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

프롬프트 앞에 `(.venv)` 가 붙으면 성공입니다.

> **실행 정책 오류가 나면** (`이 시스템에서 스크립트를 실행할 수 없으므로...`):
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> 그 다음 다시 `.\.venv\Scripts\Activate.ps1`. 이 설정은 현재 사용자에게만 적용되며 시스템 전체를 바꾸지 않습니다.

### VS Code 인터프리터 선택

`Ctrl+Shift+P` → **Python: Select Interpreter** → `.\.venv\Scripts\python.exe` 선택.
좌측 하단 상태바에 `Python 3.12.x ('.venv')` 가 표시되어야 합니다.

### 의존성

`requirements.txt` vs `pyproject.toml` — **48시간 해커톤에서는 `requirements.txt`를 쓰세요.** 패키징이 아니라 실행만 하면 되고, 팀원이 `pip install -r requirements.txt` 한 줄로 끝낼 수 있습니다.

**파일: `requirements.txt`**

```text
fastapi==0.115.6
uvicorn[standard]==0.34.0
httpx==0.28.1
pydantic==2.10.4
python-dotenv==1.0.1
google-auth==2.37.0
google-auth-oauthlib==1.2.1
google-api-python-client==2.156.0
jinja2==3.1.5
pytest==8.3.4
# 선택 (LLM 파서를 쓸 때만)
anthropic==0.42.0
# 선택 (Slack 승인을 쓸 때만)
slack-bolt==1.22.0
```

```powershell
# 위치: C:\dev\fairmeet, (.venv) 활성 상태
pip install -r requirements.txt
```

> 버전은 작성 시점 기준입니다. 설치 실패 시 버전 핀을 제거하고 다시 시도하세요.

### 비밀정보 관리

**파일: `.gitignore`**

```gitignore
# 비밀 — 절대 커밋 금지
.env
credentials.json
tokens/
*.token.json
secrets/

# 데이터
*.db
*.db-wal
*.db-shm

# Python
.venv/
__pycache__/
*.pyc
.pytest_cache/

# 에디터
.vscode/settings.json
```

**파일: `.env.example`** (이 파일은 커밋합니다)

```dotenv
# 그룹 가명 생성용 비밀키 — 팀마다 새로 생성
FAIRMEET_GROUP_SECRET=change-me-to-a-random-64-char-hex

# 에이전트 인증용 공유 토큰 (MVP 수준)
FAIRMEET_AGENT_TOKEN=change-me-too

# Mediator
FAIRMEET_DB=fairmeet.db
FAIRMEET_MEDIATOR_URL=http://127.0.0.1:8000
FAIRMEET_APPROVAL_TTL_MIN=30

# 선택: LLM 제약 파서
ANTHROPIC_API_KEY=

# 선택: Slack
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=
```

**파일: `.env`** (커밋하지 않음). 비밀키 생성:

```powershell
# 위치: 아무 곳, (.venv) 활성 상태
python -c "import secrets; print(secrets.token_hex(32))"
```

출력값을 `.env`의 `FAIRMEET_GROUP_SECRET`에 붙여넣으세요.

### 비밀 파일을 두면 안 되는 곳

| 절대 금지 | 이유 |
|---|---|
| Git 저장소 (`.env`, `credentials.json`, `tokens/`) | 공개 저장소면 즉시 유출. 한 번 커밋되면 히스토리에서 지우기 어려움 |
| Docker 이미지 레이어 (`COPY .env`) | 이미지를 받은 누구나 추출 가능 |
| 발표 슬라이드 / 화면 캡처 | 시연 중 터미널에 키가 찍히지 않는지 확인 |
| 코드 내 하드코딩 | AI 도구가 읽어 다른 파일에 복사할 수 있음 |
| Claude Code에 붙여넣기 | 대화 기록에 남음 |

> Claude Code에서 민감한 파일을 읽지 못하게 하려면 `~/.claude/settings.json`에 해당 경로에 대한 `Read` deny 규칙을 추가할 수 있습니다.

### 환경변수 로딩

**파일: `common/config.py`**

```python
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def require(name: str) -> str:
    val = os.environ.get(name)
    if not val or val.startswith("change-me"):
        raise RuntimeError(
            f"환경변수 {name} 가 설정되지 않았습니다. "
            f".env.example 을 복사해 .env 를 만들고 값을 채우세요."
        )
    return val


GROUP_SECRET = require("FAIRMEET_GROUP_SECRET")
AGENT_TOKEN = require("FAIRMEET_AGENT_TOKEN")
MEDIATOR_URL = os.environ.get("FAIRMEET_MEDIATOR_URL", "http://127.0.0.1:8000")
APPROVAL_TTL_MIN = int(os.environ.get("FAIRMEET_APPROVAL_TTL_MIN", "30"))
```

### 첫 smoke test

**파일: `tests/test_smoke.py`**

```python
def test_imports():
    import fastapi, httpx, googleapiclient, google_auth_oauthlib  # noqa
    from common import config  # noqa


def test_fairness_module_loads():
    from common.fairness import solve, build_regret_matrix  # noqa
```

```powershell
# 위치: C:\dev\fairmeet
pytest -q
```

기대 출력: `2 passed`

**완료 조건:** `pytest -q`가 통과하고, `git status`에 `.env`와 `tokens/`가 나타나지 않을 것.

## 11.9 macOS / Linux 명령 (Windows를 쓰지 않는 팀원용)

> **이 절은 대안 경로입니다.** 팀 전체가 Windows면 건너뛰세요. 한 명만 macOS를 쓴다면 이 절의 명령으로 같은 결과에 도달합니다. **두 경로를 섞지 마세요** — 특히 환경변수 문법이 다릅니다.

### 도구 설치

| 항목 | Windows (본문) | macOS / Linux |
|---|---|---|
| Claude Code CLI | `irm https://claude.ai/install.ps1 \| iex` | `curl -fsSL https://claude.ai/install.sh \| bash` |
| Python | python.org 설치 프로그램 | macOS: `brew install python@3.12` / Ubuntu: `sudo apt install python3.12 python3.12-venv` |
| VS Code | code.visualstudio.com | 동일 (또는 `brew install --cask visual-studio-code`) |
| Git | git-scm.com/downloads/win | macOS: 기본 포함 / Ubuntu: `sudo apt install git` |

VS Code 확장 설치와 로그인 절차는 §11.C와 완전히 동일합니다 (OS 무관).

### 프로젝트 초기화

```bash
mkdir -p ~/dev/fairmeet && cd ~/dev/fairmeet
git init
code .

python3 -m venv .venv
source .venv/bin/activate          # ← Windows의 .\.venv\Scripts\Activate.ps1 에 해당
pip install -r requirements.txt
```

> PowerShell의 실행 정책 문제(§20)는 macOS/Linux에 없습니다. 대신 `python3`를 명시해야 할 수 있습니다.

### 비밀키 생성

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

(OS 무관하게 동일합니다.)

### 서비스 실행 — 환경변수 문법이 다릅니다

**여기가 유일하게 헷갈리는 지점입니다.** Bash에서는 명령 앞에 인라인으로 붙일 수 있습니다.

```bash
# 터미널 1 — Mediator
cd ~/dev/fairmeet && source .venv/bin/activate
uvicorn mediator.server:app --port 8000 --reload

# 터미널 2 — Agent A
cd ~/dev/fairmeet && source .venv/bin/activate
FAIRMEET_PROFILE=profiles/a.json FAIRMEET_TOKEN=tokens/token_a.json \
  uvicorn agent.server:app --port 8001

# 터미널 3 — Agent B
FAIRMEET_PROFILE=profiles/b.json FAIRMEET_TOKEN=tokens/token_b.json \
  uvicorn agent.server:app --port 8002

# 터미널 4 — Agent C
FAIRMEET_PROFILE=profiles/c.json FAIRMEET_TOKEN=tokens/token_c.json \
  uvicorn agent.server:app --port 8003
```

**대조표:**

| 목적 | PowerShell | Bash |
|---|---|---|
| 환경변수 설정 | `$env:FAIRMEET_TOKEN = "tokens\token_a.json"` (별도 줄) | `FAIRMEET_TOKEN=tokens/token_a.json command` (인라인 가능) |
| 경로 구분자 | `\` | `/` |
| 가상환경 활성 | `.\.venv\Scripts\Activate.ps1` | `source .venv/bin/activate` |

Python 코드 안의 경로는 `pathlib.Path`를 쓰므로 OS에 관계없이 그대로 동작합니다. 문자열로 `"tokens\token_a.json"`처럼 하드코딩하지 마세요.

### 종료

```bash
# 각 터미널에서 Ctrl+C. 포트가 남으면:
lsof -ti:8000,8001,8002,8003 | xargs -r kill -9
```

### preflight (§17.4의 Bash 버전)

**파일: `scripts/preflight.sh`**

```bash
#!/usr/bin/env bash
# 발표 당일 아침에 실행. 위치: ~/dev/fairmeet
set -u
fail=0

check() {
    printf "%-46s" "$1"
    shift
    if output=$("$@" 2>&1); then
        echo "OK"
    else
        echo "FAIL"
        echo "   $output" | tail -3
        fail=$((fail + 1))
    fi
}

[ -n "${VIRTUAL_ENV:-}" ] || { echo "가상환경을 먼저 활성화하세요"; exit 1; }

check "의존성 설치"   python -c "import fastapi, googleapiclient"
check ".env 로드"     python -c "from common.config import GROUP_SECRET"
check "단위 테스트"   pytest -q -m "not live"

for u in a b c; do
    check "토큰 유효: $u" python - <<EOF
import datetime as dt
from zoneinfo import ZoneInfo
from agent.calendar_client import get_service, fetch_busy
s = get_service('tokens/token_$u.json')
n = dt.datetime.now(ZoneInfo('Asia/Seoul'))
fetch_busy(s, ['primary'], n, n + dt.timedelta(days=7))
EOF
done

check "DB 접근"       python -c "from mediator.store import init_db; init_db()"
check "베이스라인 실험" python eval/compare.py

echo
if [ "$fail" -eq 0 ]; then
    echo "전체 통과 — 시연 준비 완료"
else
    echo "$fail 개 실패 — 시연 전에 해결하세요"
    echo "토큰 실패라면: python scripts/bootstrap_tokens.py a b c"
    exit 1
fi
```

```bash
chmod +x scripts/preflight.sh
./scripts/preflight.sh
```

### WSL을 쓸 경우

WSL은 **대안 경로이지 필수 경로가 아닙니다.** WSL 안에서는 위의 Linux 명령을 그대로 쓰고, Windows 명령과 섞지 마세요. 주의할 점 두 가지:

1. **OAuth 브라우저 플로우** — `InstalledAppFlow.run_local_server()`가 WSL 안에서 브라우저를 열지 못할 수 있습니다. 이 경우 출력된 URL을 Windows 브라우저에 직접 붙여넣으세요.
2. **프로젝트 위치** — `/mnt/c/...` 가 아니라 WSL 파일시스템(`~/dev/fairmeet`) 안에 두어야 파일 I/O가 느려지지 않습니다.

Claude Code를 WSL에서 쓰려면 WSL 터미널 안에서 Linux 설치 명령을 실행하고, PowerShell이 아니라 WSL 터미널에서 `claude`를 실행합니다.

---

---

# 12. 프로젝트 구조와 파일별 책임

코드를 쓰기 전에 무엇이 어디에 사는지 확정합니다.

```text
fairmeet/
├── .env                       # 비밀 (커밋 안 함)
├── .env.example               # 템플릿 (커밋)
├── .gitignore
├── requirements.txt
├── credentials.json           # Google OAuth 클라이언트 (커밋 안 함)
├── fairmeet.db                # SQLite (커밋 안 함)
│
├── common/
│   ├── config.py              # 환경변수 로딩, 필수값 검증
│   ├── slots.py               # 슬롯 그리드 생성 (시간대 처리 포함)
│   ├── fairness.py            # ★ 공정성 solver + 베이스라인 + gini
│   ├── protocol.py            # Pydantic 메시지 스키마
│   └── ids.py                 # HMAC 그룹 가명 생성
│
├── agent/
│   ├── calendar_client.py     # ★ OAuth, freebusy, 멱등 이벤트 생성
│   ├── cost.py                # 비용 함수 (개인정보에 접근하는 유일한 곳)
│   ├── profile.py             # 프로필 로딩 + 쿼터 검증
│   ├── nl_parser.py           # 선택: LLM 제약 파서 + 폼 fallback
│   └── server.py              # FastAPI :8001~
│
├── mediator/
│   ├── schema.sql             # DB 스키마
│   ├── store.py               # ★ 상태 저장 및 전이
│   ├── payload_guard.py       # 허용 필드 검증
│   ├── negotiation.py         # 협상 라운드 루프
│   ├── commit.py              # ★ 재확인 + 단일 멱등 커밋 오케스트레이션
│   ├── approval_web.py        # ★ 웹 승인 (MVP 기본 경로)
│   └── server.py              # FastAPI :8000
│
├── slackbot/
│   └── handler.py             # 선택: Slack 승인 (웹의 대안)
│
├── dashboard/
│   └── index.html             # SSE 협상 로그 + 잔액 시각화
│
├── eval/
│   └── compare.py             # 4개 전략 비교 실험
│
├── scripts/
│   ├── preflight.ps1          # 발표 전 전체 점검
│   └── bootstrap_tokens.py    # 사용자별 OAuth 토큰 발급
│
├── profiles/                  # a.json, b.json, c.json
├── tokens/                    # token_a.json 등 (커밋 안 함)
└── tests/
```

`★` 표시가 프롬프트에서 완전한 코드를 요구한 4개 모듈입니다.

---

# 13. Google Calendar API 설정 및 실제 smoke test

## 13.1 Google Cloud Console 설정 (순서대로)

### 1) 프로젝트 생성

1. https://console.cloud.google.com 접속
2. 상단 프로젝트 선택기 → **새 프로젝트**
3. 이름: `fairmeet-makerthon` → **만들기**
4. 생성 후 프로젝트 선택기에서 이 프로젝트로 전환됐는지 확인

### 2) Calendar API 활성화

1. 좌측 메뉴 → **API 및 서비스** → **라이브러리**
2. "Google Calendar API" 검색 → 클릭 → **사용** 버튼

### 3) OAuth 동의 화면

1. **API 및 서비스** → **OAuth 동의 화면**
2. User Type: **외부(External)** → 만들기
3. 앱 이름 `FairMeet`, 사용자 지원 이메일, 개발자 연락처 이메일 입력
4. **게시 상태는 "테스트(Testing)"로 유지하세요.** 프로덕션 게시는 Google 검증이 필요하고 수 주가 걸립니다

### 4) 테스트 사용자 등록 ← **이걸 놓치면 발표 당일 막힙니다**

1. OAuth 동의 화면 → **테스트 사용자** → **ADD USERS**
2. 시연에 쓸 Gmail 주소를 **전부** 입력 (팀원 3명 + 예비 1개)
3. 저장

여기에 없는 계정으로 로그인하면 `access_denied` 오류가 납니다.

### 5) 스코프 선정

```python
SCOPES = [
    "https://www.googleapis.com/auth/calendar.freebusy",      # 가용성 조회
    "https://www.googleapis.com/auth/calendar.events.owned",  # 소유 캘린더 이벤트
]
```

공식 문서의 정의:

| 스코프 | 공식 설명 |
|---|---|
| `calendar.freebusy` | 자신의 캘린더에서 가용성(availability)을 조회 |
| `calendar.events.owned` | **자신이 소유한** Google 캘린더의 이벤트를 조회·생성·수정·삭제 |
| `calendar.events` | **모든** 캘린더의 이벤트를 조회·수정 (더 넓음) |
| `calendar.app.created` | **보조(secondary) 캘린더를 생성**하고 그 위의 이벤트를 관리 |

출처: [Choose Google Calendar API scopes](https://developers.google.com/workspace/calendar/api/auth)

**정직하게 말해야 할 세 가지:**

1. `calendar.events.owned`는 `calendar.events`보다 좁지만, **여전히 소유 캘린더의 이벤트를 읽을 수 있습니다.** "API 권한 수준에서 제목을 읽을 수 없다"는 주장은 성립하지 않습니다.
2. 정확한 서술은 이것입니다: **"조회에는 `freebusy`만 사용하고, 애플리케이션 코드에서 `events.list`/`events.get`을 한 번도 호출하지 않습니다. 이것은 애플리케이션 수준의 통제이며, 코드 리뷰와 테스트로 검증됩니다."**
3. `calendar.app.created`는 **앱이 만든 보조 캘린더 전용**이므로 사용자의 primary 캘린더에 이벤트를 기록하는 용도로 쓸 수 없습니다. 혼동하지 마세요.

**애플리케이션 수준 통제를 검증하는 테스트:**

```python
# tests/test_no_event_read.py
import ast, pathlib

FORBIDDEN = {"list", "get", "instances"}


def test_agent_never_reads_events():
    """agent/ 하위 코드에서 events().list/get 호출이 없는지 정적 검사."""
    for py in pathlib.Path("agent").rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (isinstance(f, ast.Attribute) and f.attr in FORBIDDEN
                    and isinstance(f.value, ast.Call)
                    and isinstance(f.value.func, ast.Attribute)
                    and f.value.func.attr == "events"):
                raise AssertionError(f"{py}: events().{f.attr}() 호출 발견")
```

### 6) OAuth 클라이언트 생성

1. **API 및 서비스** → **사용자 인증 정보** → **사용자 인증 정보 만들기** → **OAuth 클라이언트 ID**
2. 애플리케이션 유형: **데스크톱 앱**

   > **왜 웹 앱이 아닌가:** MVP에서 각 에이전트는 로컬 프로세스이고, `InstalledAppFlow.run_local_server()`가 임의 포트로 리디렉션을 받아 `redirect_uri_mismatch`를 피할 수 있습니다. 웹 앱 유형을 고르면 리디렉션 URI를 미리 등록해야 하고 포트가 바뀔 때마다 오류가 납니다.

3. 이름: `fairmeet-desktop` → 만들기
4. **JSON 다운로드** → 프로젝트 루트에 `credentials.json` 으로 저장
5. ⚠️ `.gitignore`에 `credentials.json` 이 들어 있는지 **지금 확인하세요**

### 7) 사용자별 토큰 발급

**파일: `scripts/bootstrap_tokens.py`**

```python
"""사용자별 OAuth 토큰을 대화형으로 발급한다. 시연 전날 반드시 다시 실행할 것."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.calendar_client import get_service, SCOPES  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python scripts/bootstrap_tokens.py a b c")
        sys.exit(1)

    Path("tokens").mkdir(exist_ok=True)
    for label in sys.argv[1:]:
        token_path = Path("tokens") / f"token_{label}.json"
        print(f"\n=== {label} 계정 인증 — 브라우저가 열립니다 ===")
        svc = get_service(str(token_path))
        me = svc.calendars().get(calendarId="primary").execute()
        print(f"  ✓ {label}: {me.get('id')}  →  {token_path}")
```

```powershell
# 위치: C:\dev\fairmeet, (.venv) 활성
python scripts\bootstrap_tokens.py a b c
```

각 계정마다 브라우저가 열립니다. "이 앱은 Google에서 확인하지 않았습니다" 경고가 나오면 **고급 → FairMeet(으)로 이동(안전하지 않음)** 을 클릭하세요. 테스트 모드에서는 정상 동작입니다.

생성되는 파일: `tokens/token_a.json`, `token_b.json`, `token_c.json` — **전부 커밋 금지.**

### 8) ⚠️ 토큰 만료 — 가장 위험한 실패 지점

공식 문서에 명시된 내용:

> OAuth 동의 화면이 **외부(External)** 사용자 유형이고 게시 상태가 **"테스트(Testing)"** 인 Google Cloud 프로젝트는 **7일 후 만료되는 refresh token**을 발급받습니다. (이름·이메일·프로필 스코프만 요청하는 경우는 예외)

또한 refresh token은 다음 경우에도 동작을 멈춥니다: 사용자가 앱 접근을 취소했을 때, 6개월간 사용되지 않았을 때, 계정당 OAuth 클라이언트 ID별 refresh token 100개 한도를 초과했을 때(가장 오래된 것부터 무효화).

출처: [Using OAuth 2.0 to Access Google APIs](https://developers.google.com/identity/protocols/oauth2)

**대응 절차:**

| 시점 | 행동 |
|---|---|
| 해커톤 시작 | 토큰 최초 발급 |
| **시연 전날 밤** | `python scripts\bootstrap_tokens.py a b c` **재실행** (필수) |
| **시연 당일 아침** | `python scripts\preflight.ps1` 로 전원 FreeBusy 호출 성공 확인 |
| 개발 중 `invalid_grant` 발생 | 해당 `tokens/token_*.json` 삭제 후 재발급 |

## 13.2 Calendar 클라이언트 전체 코드

**파일: `agent/calendar_client.py`**

```python
"""Google Calendar 접근. 조회는 FreeBusy만, 쓰기는 organizer 1회 멱등 insert.

이 파일은 events().list / events().get 을 호출하지 않는다.
tests/test_no_event_read.py 가 이를 정적으로 검사한다.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = [
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.events.owned",
]

CREDENTIALS_FILE = "credentials.json"

# Google Calendar 커스텀 event ID 규칙:
#   base32hex 인코딩 — 소문자 a-v 와 숫자 0-9 만, 길이 5~1024
_EVENT_ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")


class CalendarUnavailable(RuntimeError):
    """FreeBusy 조회 실패. 절대 '한가함'으로 해석하지 말 것 (fail-closed)."""


class StaleConflict(RuntimeError):
    """커밋 직전 재확인에서 충돌 발견."""


# ---- 인증 ---------------------------------------------------------------

def get_service(token_path: str):
    """사용자별 토큰으로 Calendar 서비스를 만든다.

    토큰이 없거나 만료되어 갱신 불가하면 브라우저 인증 플로우를 연다.
    시연 중에는 이 플로우가 열리면 안 되므로 preflight에서 미리 확인한다.
    """
    p = Path(token_path)
    creds = None
    if p.exists():
        creds = Credentials.from_authorized_user_file(str(p), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise CalendarUnavailable(
                    f"{token_path} 갱신 실패: {exc}. "
                    f"scripts/bootstrap_tokens.py 로 재발급하세요. "
                    f"(테스트 모드 앱의 refresh token은 7일 후 만료됩니다)"
                ) from exc
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(creds.to_json(), encoding="utf-8")

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ---- 조회 (FreeBusy만) ----------------------------------------------------

def fetch_busy(service, calendar_ids: list[str],
               time_min: dt.datetime, time_max: dt.datetime,
               tz: str = "Asia/Seoul") -> dict[str, list[tuple]]:
    """캘린더별 busy 구간을 반환한다. 일정 제목은 반환되지 않는다.

    Raises:
        CalendarUnavailable: API 실패 또는 개별 캘린더 오류.
            실패를 '한가함'으로 처리하면 사람이 실제로 바쁜 시간에
            회의를 잡게 되므로, 반드시 fail-closed 로 상위에 전파한다.
    """
    body = {
        "timeMin": time_min.isoformat(),
        "timeMax": time_max.isoformat(),
        "timeZone": tz,
        "items": [{"id": cid} for cid in calendar_ids],
    }
    try:
        resp = service.freebusy().query(body=body).execute()
    except HttpError as exc:
        raise CalendarUnavailable(f"FreeBusy 조회 실패: {exc}") from exc

    out: dict[str, list[tuple]] = {}
    for cid, data in resp.get("calendars", {}).items():
        if data.get("errors"):
            raise CalendarUnavailable(f"캘린더 {cid} 오류: {data['errors']}")
        out[cid] = [
            (dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
             dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
            for b in data.get("busy", [])
        ]
    for cid in calendar_ids:
        out.setdefault(cid, [])
    return out


def has_conflict(service, calendar_ids: list[str],
                 start: dt.datetime, end: dt.datetime) -> bool:
    """단일 구간에 대한 충돌 여부. 커밋 직전 재확인에 사용."""
    busy = fetch_busy(service, calendar_ids, start, end)
    for blocks in busy.values():
        for b0, b1 in blocks:
            if start < b1 and b0 < end:
                return True
    return False


# ---- 쓰기 (organizer 1회, 멱등) --------------------------------------------

def make_event_id(session_id: str) -> str:
    """협상 세션 ID로부터 결정론적 event ID를 만든다.

    Google Calendar 커스텀 event ID는 base32hex (a-v, 0-9), 길이 5~1024.
    UUID hex(0-9a-f)는 이 범위에 포함되므로 그대로 쓸 수 있다.

    같은 ID로 다시 insert 하면 409가 돌아오므로 이것이 멱등성 키가 된다.
    """
    eid = "fm" + session_id.replace("-", "").lower()
    if not _EVENT_ID_RE.fullmatch(eid):
        raise ValueError(f"잘못된 event ID: {eid}")
    return eid


def commit_event(service, *, session_id: str, summary: str,
                 start: dt.datetime, end: dt.datetime,
                 attendee_emails: list[str], tz: str = "Asia/Seoul",
                 description: str = "") -> tuple[str, bool]:
    """organizer 캘린더에 이벤트를 단 한 번 생성한다.

    나머지 참가자는 attendees 로 초대되므로 이벤트는 하나만 존재하고,
    Meet 링크도 하나만 만들어지며, 알림도 한 번만 간다.

    Returns:
        (event_id, created)
        created=False 는 '이미 존재했음' — 재시도 시 정상 경로다.
    """
    event_id = make_event_id(session_id)
    body = {
        "id": event_id,
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat(), "timeZone": tz},
        "end": {"dateTime": end.isoformat(), "timeZone": tz},
        "attendees": [{"email": e} for e in attendee_emails],
        "conferenceData": {
            "createRequest": {
                "requestId": event_id,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
        "reminders": {"useDefault": True},
    }
    try:
        created = service.events().insert(
            calendarId="primary",
            body=body,
            conferenceDataVersion=1,
            sendUpdates="all",
        ).execute()
        return created["id"], True
    except HttpError as exc:
        if exc.resp.status == 409:
            # 이미 같은 ID의 이벤트가 있음 = 이전 시도가 성공했음
            return event_id, False
        raise
```

## 13.3 커밋 오케스트레이션 — 재확인 + 단일 멱등 커밋

**파일: `mediator/commit.py`**

```python
"""승인 완료 후: 전원 캘린더 재확인 → organizer 1회 커밋 → 잔액 갱신.

이 순서는 바뀌면 안 된다.
  - 재확인을 건너뛰면 승인 대기 중에 생긴 일정 위에 회의를 얹는다 (stale commit)
  - 커밋 전에 잔액을 갱신하면 실패 시 잘못된 부담이 기록된다
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from agent.calendar_client import (
    CalendarUnavailable, commit_event, get_service, has_conflict,
)
from mediator import store

log = logging.getLogger("fairmeet.commit")


def finalize(session_id: str, agent_token_paths: dict[str, str]) -> dict:
    """PENDING_HUMAN_APPROVAL(전원 승인) → COMMITTED 까지 수행한다.

    Args:
        agent_token_paths: agent_id -> tokens/token_x.json

    Returns:
        {"state": ..., "event_id": ..., "reason": ...}
    """
    row = store.get_session(session_id)
    if row is None:
        return {"state": "UNKNOWN", "reason": "session not found"}

    parts = store.get_participants(session_id)
    slots = json.loads(row["slots_json"])
    start_iso, end_iso = slots[row["proposed_slot"]]
    start = dt.datetime.fromisoformat(start_iso)
    end = dt.datetime.fromisoformat(end_iso)

    # --- 1) 재확인 상태로 전이 (조건부 → 중복 실행 차단) ---
    if not store.transition(session_id, "PENDING_HUMAN_APPROVAL",
                            "RECHECKING_CALENDARS"):
        cur = store.get_session(session_id)["state"]
        log.info("finalize: 이미 %s 상태, 건너뜀", cur)
        return {"state": cur, "reason": "already advanced"}

    # --- 2) 전원 FreeBusy 재조회 ---
    try:
        for p in parts:
            svc = get_service(agent_token_paths[p["agent_id"]])
            if has_conflict(svc, ["primary"], start, end):
                store.transition(session_id, "RECHECKING_CALENDARS",
                                 "STALE_CONFLICT",
                                 fail_reason=f"{p['display_name']} 일정 충돌")
                return {"state": "STALE_CONFLICT",
                        "reason": f"{p['display_name']}의 캘린더가 변경되었습니다"}
    except CalendarUnavailable as exc:
        store.transition(session_id, "RECHECKING_CALENDARS",
                         "AGENT_UNAVAILABLE", fail_reason=str(exc))
        return {"state": "AGENT_UNAVAILABLE", "reason": str(exc)}

    # --- 3) organizer 캘린더에 1회 커밋 ---
    if not store.transition(session_id, "RECHECKING_CALENDARS", "COMMITTING"):
        return {"state": store.get_session(session_id)["state"],
                "reason": "concurrent"}

    organizer = next((p for p in parts if p["is_organizer"]), parts[0])
    attendees = [p["email"] for p in parts if p["agent_id"] != organizer["agent_id"]]

    try:
        svc = get_service(agent_token_paths[organizer["agent_id"]])
        event_id, created = commit_event(
            svc,
            session_id=session_id,
            summary="FairMeet 협의 일정",
            start=start, end=end,
            attendee_emails=attendees,
            description="FairMeet 에이전트 협상으로 확정된 일정입니다.",
        )
    except Exception as exc:
        log.exception("커밋 실패")
        store.transition(session_id, "COMMITTING", "COMMIT_FAILED",
                         fail_reason=str(exc))
        return {"state": "COMMIT_FAILED", "reason": str(exc)}

    store.transition(session_id, "COMMITTING", "COMMITTED",
                     google_event_id=event_id)

    # --- 4) 커밋 성공 뒤에만 잔액 갱신 (정확히 1회) ---
    store.apply_balance_once(session_id)

    log.info("커밋 완료 session=%s event=%s created=%s",
             session_id, event_id, created)
    return {"state": "COMMITTED", "event_id": event_id, "newly_created": created}
```

## 13.4 smoke test

**파일: `tests/test_calendar_smoke.py`** (실제 토큰이 필요하므로 별도 마커)

```python
"""실제 API를 호출한다. `pytest -m live` 로만 실행."""
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.calendar_client import (
    fetch_busy, get_service, make_event_id, commit_event,
)

KST = ZoneInfo("Asia/Seoul")
pytestmark = pytest.mark.live


@pytest.mark.skipif(not Path("tokens/token_a.json").exists(),
                    reason="토큰 없음 — bootstrap_tokens.py 먼저 실행")
def test_freebusy_returns_no_titles():
    svc = get_service("tokens/token_a.json")
    now = dt.datetime.now(KST)
    busy = fetch_busy(svc, ["primary"], now, now + dt.timedelta(days=7))
    assert "primary" in busy
    for b0, b1 in busy["primary"]:
        assert isinstance(b0, dt.datetime) and b1 > b0
    print(f"busy 구간 {len(busy['primary'])}개 — 제목 필드 없음 확인")


def test_event_id_is_deterministic_and_valid():
    sid = "8f2a1b3c4d5e6f708192a3b4c5d6e7f8"
    assert make_event_id(sid) == make_event_id(sid)
    assert make_event_id(sid).startswith("fm")


@pytest.mark.skipif(not Path("tokens/token_a.json").exists(), reason="토큰 없음")
def test_double_insert_is_idempotent():
    """같은 session_id로 두 번 커밋해도 이벤트는 하나만 생긴다."""
    svc = get_service("tokens/token_a.json")
    sid = "f" * 32
    start = dt.datetime.now(KST).replace(
        hour=23, minute=0, second=0, microsecond=0) + dt.timedelta(days=30)
    end = start + dt.timedelta(minutes=30)

    eid1, created1 = commit_event(svc, session_id=sid, summary="멱등성 테스트",
                                  start=start, end=end, attendee_emails=[])
    eid2, created2 = commit_event(svc, session_id=sid, summary="멱등성 테스트",
                                  start=start, end=end, attendee_emails=[])
    assert eid1 == eid2
    assert created1 is True and created2 is False

    svc.events().delete(calendarId="primary", eventId=eid1).execute()
```

`pyproject.toml` 또는 `pytest.ini`에 마커 등록:

```ini
[pytest]
markers =
    live: 실제 외부 API를 호출하는 테스트
```

```powershell
# 위치: C:\dev\fairmeet
pytest -q -m "not live"     # 평소
pytest -q -m live           # 토큰 발급 후 1회
```

**완료 조건:** `busy 구간 N개` 가 출력되고, 멱등성 테스트가 통과할 것.

---

# 14. 승인 UI — 웹(기본) 및 Slack(선택)

## 14.1 MVP 권장: 웹 승인 페이지

**Slack을 48시간 MVP에서 빼는 것을 권장합니다.** 이유는 실패 지점이 네 개(터널 URL 변동, 서명 검증, 3초 제한, 봇 스코프)나 되고, 전부 Slack 고유 문제라서 프로젝트의 본질과 무관하기 때문입니다.

**파일: `mediator/approval_web.py`**

```python
"""참가자별 1회용 링크로 비공개 승인을 받는다.

공개 채널에 누가 승인했는지 표시하지 않는다 — 미승인자를 노출하면
바로 그 권력 압박이 재현되기 때문이다.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from common.config import APPROVAL_TTL_MIN
from mediator import store

router = APIRouter()

PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FairMeet 승인</title>
<style>
 body{{font-family:system-ui,-apple-system,"Malgun Gothic",sans-serif;
      max-width:32rem;margin:3rem auto;padding:0 1.25rem;line-height:1.6;color:#1a1a1a}}
 .slot{{font-size:1.5rem;font-weight:600;margin:1.5rem 0;padding:1rem;
        background:#f4f6f8;border-radius:.5rem}}
 button{{font-size:1rem;padding:.7rem 1.6rem;margin-right:.6rem;
         border-radius:.4rem;border:1px solid #ccc;cursor:pointer}}
 .ok{{background:#0b7;color:#fff;border-color:#0b7}}
 .no{{background:#fff;color:#c33;border-color:#c33}}
 .note{{color:#666;font-size:.9rem;margin-top:2rem}}
</style></head><body>
<h2>제안된 시간</h2>
<div class="slot">{slot}</div>
<p>{name} 님, 이 시간에 동의하시나요?</p>
<p class="note">동의하지 않으셔도 <strong>이유를 설명할 필요가 없습니다.</strong>
거절하면 협상이 다시 시작됩니다.</p>
<form method="post">
  <button class="ok" name="verdict" value="approve" type="submit">동의합니다</button>
  <button class="no" name="verdict" value="reject" type="submit">거절합니다</button>
</form>
<p class="note">이 링크는 {expires} 까지 유효합니다.<br>
다른 참가자의 응답은 표시되지 않습니다.</p>
</body></html>"""

DONE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>FairMeet</title><style>
body{{font-family:system-ui,"Malgun Gothic",sans-serif;max-width:32rem;
margin:4rem auto;padding:0 1.25rem;line-height:1.7}}</style></head>
<body><h2>{title}</h2><p>{body}</p></body></html>"""


def _lookup(token: str):
    """승인 토큰으로 (session_row, participant_row) 를 찾는다."""
    with store.connect() as c:
        p = c.execute("SELECT * FROM participants WHERE approval_token = ?",
                      (token,)).fetchone()
    if p is None:
        raise HTTPException(404, "유효하지 않은 링크입니다")
    s = store.get_session(p["session_id"])
    if s is None:
        raise HTTPException(404, "세션을 찾을 수 없습니다")
    return s, p


def _slot_text(session_row) -> str:
    slots = json.loads(session_row["slots_json"])
    idx = session_row["proposed_slot"]
    if idx is None:
        return "(미정)"
    start = dt.datetime.fromisoformat(slots[idx][0])
    end = dt.datetime.fromisoformat(slots[idx][1])
    wd = "월화수목금토일"[start.weekday()]
    return f"{start:%m월 %d일}({wd}) {start:%H:%M} – {end:%H:%M}"


@router.get("/approve/{token}", response_class=HTMLResponse)
def show(token: str):
    s, p = _lookup(token)

    if s["state"] != "PENDING_HUMAN_APPROVAL":
        return HTMLResponse(DONE.format(
            title="승인 기간이 지났습니다",
            body=f"현재 상태: {s['state']}"))

    with store.connect() as c:
        existing = c.execute(
            "SELECT verdict FROM approvals WHERE session_id=? AND agent_id=?",
            (s["session_id"], p["agent_id"])).fetchone()
    if existing:
        return HTMLResponse(DONE.format(
            title="이미 응답하셨습니다",
            body="응답: " + ("동의" if existing["verdict"] == "approve" else "거절")))

    expires = dt.datetime.fromisoformat(s["expires_at"]).astimezone()
    return HTMLResponse(PAGE.format(
        slot=_slot_text(s), name=p["display_name"],
        expires=f"{expires:%m월 %d일 %H:%M}"))


@router.post("/approve/{token}", response_class=HTMLResponse)
async def decide(token: str, request: Request, verdict: str = Form(...)):
    if verdict not in ("approve", "reject"):
        raise HTTPException(400, "잘못된 값")

    s, p = _lookup(token)
    session_id = s["session_id"]

    result = store.record_approval(session_id, p["agent_id"], verdict)
    if result == "duplicate":
        return HTMLResponse(DONE.format(title="이미 응답하셨습니다", body=""))
    if result == "not_pending":
        return HTMLResponse(DONE.format(
            title="승인 기간이 지났습니다",
            body="협상이 다시 시작되었거나 만료되었습니다."))

    if verdict == "reject":
        store.transition(session_id, "PENDING_HUMAN_APPROVAL", "REJECTED",
                         fail_reason="participant rejected")
        return HTMLResponse(DONE.format(
            title="거절이 접수되었습니다",
            body="이유는 다른 참가자에게 전달되지 않습니다. "
                 "에이전트가 다른 시간을 찾습니다."))

    ok, no, total = store.approval_tally(session_id)
    if ok == total:
        # 전원 승인 → 백그라운드로 재확인+커밋
        from mediator.server import schedule_finalize
        schedule_finalize(session_id)
        return HTMLResponse(DONE.format(
            title="전원 동의 완료",
            body="캘린더를 다시 확인한 뒤 일정을 등록합니다."))

    return HTMLResponse(DONE.format(
        title="동의가 기록되었습니다",
        body="다른 참가자의 응답을 기다리는 중입니다. "
             "누가 아직 응답하지 않았는지는 표시하지 않습니다."))


def open_approval_window(session_id: str, slot_index: int, regrets: dict) -> list[dict]:
    """승인 대기 상태로 전이하고 참가자별 링크를 반환한다."""
    expires = (dt.datetime.now(dt.timezone.utc)
               + dt.timedelta(minutes=APPROVAL_TTL_MIN)).isoformat()
    ok = store.transition(
        session_id, "AGENT_NEGOTIATING", "PENDING_HUMAN_APPROVAL",
        proposed_slot=slot_index,
        proposed_regrets=json.dumps(regrets),
        expires_at=expires,
    )
    if not ok:
        return []
    from common.config import MEDIATOR_URL
    return [
        {"display_name": p["display_name"], "email": p["email"],
         "url": f"{MEDIATOR_URL}/approve/{p['approval_token']}"}
        for p in store.get_participants(session_id)
    ]
```

**smoke test:**

```powershell
# 터미널 1 (위치: C:\dev\fairmeet)
uvicorn mediator.server:app --port 8000
```

브라우저에서 `http://127.0.0.1:8000/approve/<token>` 접속 → 슬롯이 표시되고 버튼 두 개가 보이면 성공. 같은 링크를 다시 열면 "이미 응답하셨습니다"가 나와야 합니다.

## 14.2 선택: Slack 승인

### Slack 앱 설정 (메뉴 경로)

1. https://api.slack.com/apps → **Create New App** → **From scratch**
2. 이름 `FairMeet`, 워크스페이스 선택
3. **OAuth & Permissions** → Scopes → **Bot Token Scopes**에 추가:
   - `commands` — slash command
   - `chat:write` — 메시지 전송
   - `im:write` — DM 전송 (**승인은 DM으로 받습니다**)
   - `users:read` / `users:read.email` — Slack 사용자와 이메일 매칭
4. 페이지 상단 **Install to Workspace** → 승인 → **Bot User OAuth Token**(`xoxb-...`) 복사 → `.env`의 `SLACK_BOT_TOKEN`
5. **Basic Information** → **Signing Secret** 복사 → `.env`의 `SLACK_SIGNING_SECRET`
6. **Slash Commands** → **Create New Command** → `/meet`, Request URL: `https://<터널>/slack/events`
7. **Interactivity & Shortcuts** → 켜기 → Request URL: `https://<터널>/slack/events`

### 터널

```powershell
# 별도 터미널
ngrok http 8000
```

출력되는 `https://xxxx.ngrok-free.app` 를 6·7번 두 곳에 **모두** 입력합니다.

> ⚠️ 무료 ngrok은 **재시작할 때마다 URL이 바뀝니다.** 바뀌면 Slash Command와 Interactivity 두 곳을 다시 수정해야 합니다. 시연 중에 ngrok을 재시작하지 마세요.

### 코드

**파일: `slackbot/handler.py`**

```python
"""Slack 승인. 핵심 규칙 세 가지:
  1) ack() 를 3초 안에 가장 먼저 호출한다
  2) 버튼 value 에는 session_id 만 담고, 나머지 상태는 DB에서 읽는다
  3) 승인 요청은 DM 으로 보내고, 채널에는 개인별 응답을 노출하지 않는다
"""
from __future__ import annotations

import json
import os
import threading
import datetime as dt

from slack_bolt import App
from slack_bolt.adapter.fastapi import SlackRequestHandler

from mediator import store
from mediator.approval_web import _slot_text

# signing_secret 을 넘기면 Bolt 가 요청 서명을 자동 검증한다
app = App(token=os.environ["SLACK_BOT_TOKEN"],
          signing_secret=os.environ["SLACK_SIGNING_SECRET"])
slack_handler = SlackRequestHandler(app)


@app.command("/meet")
def handle_meet(ack, body, client, logger):
    # 공식 권장: 시간이 걸리는 작업을 시작하기 전에 ack() 를 즉시 호출.
    # 3초 안에 응답하지 않으면 Slack 이 timeout 으로 처리한다.
    ack("에이전트들이 협상을 시작했습니다. 결과는 개별 DM 으로 보내드립니다.")

    channel_id = body["channel_id"]
    threading.Thread(
        target=_run_negotiation_bg,
        args=(channel_id, client, logger),
        daemon=True,
    ).start()


def _run_negotiation_bg(channel_id, client, logger):
    """3초 제한 밖에서 실제 작업을 수행한다."""
    try:
        from mediator.negotiation import start_negotiation
        session_id, links = start_negotiation(group_id=channel_id, mode="work")
        if not links:
            client.chat_postMessage(
                channel=channel_id,
                text="모두가 가능한 시간을 찾지 못했습니다. 직접 정해 주세요.")
            return
        _send_approval_dms(session_id, client, logger)
        client.chat_postMessage(
            channel=channel_id,
            text="참가자 전원에게 승인 요청 DM 을 보냈습니다.")
    except Exception:
        logger.exception("협상 실패")
        client.chat_postMessage(channel=channel_id,
                                text="협상 중 오류가 발생했습니다.")


def _send_approval_dms(session_id, client, logger):
    """참가자별 DM. 채널에는 아무것도 노출하지 않는다."""
    s = store.get_session(session_id)
    for p in store.get_participants(session_id):
        try:
            u = client.users_lookupByEmail(email=p["email"])
            client.chat_postMessage(
                channel=u["user"]["id"],
                text=f"제안된 시간: {_slot_text(s)}",
                blocks=[
                    {"type": "section",
                     "text": {"type": "mrkdwn",
                              "text": f"*제안된 시간*\n{_slot_text(s)}\n\n"
                                      f"_거절하셔도 이유를 설명할 필요가 없습니다._"}},
                    {"type": "actions",
                     "elements": [
                         {"type": "button", "style": "primary",
                          "text": {"type": "plain_text", "text": "동의"},
                          "action_id": "fm_approve",
                          "value": session_id},
                         {"type": "button",
                          "text": {"type": "plain_text", "text": "거절"},
                          "action_id": "fm_reject",
                          "value": session_id},
                     ]},
                ])
        except Exception:
            logger.exception("DM 실패: %s", p["email"])


def _decide(ack, body, client, verdict):
    ack()                                   # 무조건 먼저
    session_id = body["actions"][0]["value"]
    slack_user = body["user"]["id"]

    info = client.users_info(user=slack_user)
    email = info["user"]["profile"].get("email")

    agent_id = None
    for p in store.get_participants(session_id):
        if p["email"] == email:
            agent_id = p["agent_id"]
            break
    if agent_id is None:
        client.chat_postEphemeral(channel=slack_user, user=slack_user,
                                  text="이 협상의 참가자가 아닙니다.")
        return

    result = store.record_approval(session_id, agent_id, verdict)

    if result == "duplicate":
        msg = "이미 응답하셨습니다."
    elif result == "not_pending":
        msg = "승인 기간이 지났습니다."
    elif verdict == "reject":
        store.transition(session_id, "PENDING_HUMAN_APPROVAL", "REJECTED",
                         fail_reason="participant rejected")
        msg = "거절이 접수되었습니다. 이유는 공유되지 않습니다."
    else:
        ok, no, total = store.approval_tally(session_id)
        if ok == total:
            from mediator.server import schedule_finalize
            schedule_finalize(session_id)
            msg = "전원 동의. 캘린더를 확인한 뒤 등록합니다."
        else:
            msg = "동의가 기록되었습니다. 다른 분들의 응답을 기다립니다."

    # 원본 DM 을 갱신 — 채널이 아니라 본인에게만 보임
    client.chat_update(channel=body["channel"]["id"],
                       ts=body["message"]["ts"], text=msg, blocks=[])


@app.action("fm_approve")
def on_approve(ack, body, client):
    _decide(ack, body, client, "approve")


@app.action("fm_reject")
def on_reject(ack, body, client):
    _decide(ack, body, client, "reject")
```

FastAPI에 연결:

```python
# mediator/server.py 에 추가 (Slack 을 쓸 때만)
from fastapi import Request

try:
    from slackbot.handler import slack_handler

    @app.post("/slack/events")
    async def slack_events(req: Request):
        return await slack_handler.handle(req)
except (ImportError, KeyError):
    pass   # Slack 미설정 시 조용히 건너뜀
```

### Slack smoke test

1. Slack 채널에서 `/meet` 입력
2. **3초 안에** "협상을 시작했습니다" 응답이 와야 함 → 안 오면 `dispatch_failed`
3. DM 으로 승인 버튼이 도착
4. 버튼을 두 번 눌러도 "이미 응답하셨습니다"가 나오는지 확인
5. 채널에 개인별 승인 여부가 **표시되지 않는지** 확인

---

# 15. 애플리케이션용 LLM API 설정과 fallback

## 15.1 역할 제한 — 이 경계를 넘지 마세요

| LLM이 하는 일 | LLM이 절대 하지 않는 일 |
|---|---|
| "금요일 저녁은 알바야" → 구조화된 제약 후보로 변환 | 확정 시간 계산 |
| 변환 결과를 사용자에게 보여주고 확인받기 | 승인 판정 |
| | Calendar commit |
| | 비용 점수 산출 |

**캘린더 제목이나 일정 데이터를 LLM에 보내지 않습니다.** LLM이 보는 것은 사용자가 직접 입력한 자연어 문장 하나뿐입니다.

## 15.2 설정

1. https://console.anthropic.com → API Keys → **Create Key**
2. Settings → Limits 에서 **월 지출 한도**를 먼저 설정
3. `.env`의 `ANTHROPIC_API_KEY`에 저장

> **다시 강조:** Claude Code 로그인과 이 API 키는 별개의 과금 경로입니다.

## 15.3 코드 — 검증과 fallback

**파일: `agent/nl_parser.py`**

```python
"""자연어 제약을 구조화한다. API 없이도 폼 입력으로 100% 대체 가능하다."""
from __future__ import annotations

import json
import os
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

WEEKDAYS = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}


class ParsedConstraint(BaseModel):
    kind: Literal["protected_window"]
    weekday: int = Field(ge=0, le=6)
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    label: str = Field(max_length=20, default="보호 시간")


SYSTEM = """사용자의 일정 제약 문장을 JSON 배열로 변환하세요.
각 원소: {"kind":"protected_window","weekday":0-6(월=0),
"start":"HH:MM","end":"HH:MM","label":"짧은 이름"}
JSON 배열만 출력하고 다른 설명은 붙이지 마세요.
확실하지 않으면 빈 배열 []을 출력하세요."""

FENCE = chr(96) * 3        # 백틱 3개. 마크다운 코드블록과 충돌하지 않도록 이렇게 씁니다


def _strip_code_fence(raw: str) -> str:
    """LLM이 응답을 코드블록으로 감싸는 경우를 벗겨낸다."""
    if not raw.startswith(FENCE):
        return raw
    body = raw.split(FENCE)
    if len(body) < 2:
        return raw
    inner = body[1]
    if inner.startswith("json"):
        inner = inner[4:]
    return inner.strip()


def parse_with_llm(text: str) -> list[ParsedConstraint] | None:
    """성공하면 리스트, API 없음/실패/검증 실패면 None (→ 폼으로 fallback)."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=512,
            system=SYSTEM,
            messages=[{"role": "user", "content": text[:500]}],
        )
        raw = _strip_code_fence(resp.content[0].text.strip())
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        return [ParsedConstraint(**item) for item in data]
    except Exception:
        # ImportError / ValidationError / JSONDecodeError / API 오류 전부
        # 동일하게 처리한다 — LLM 경로는 어떤 이유로든 실패하면 폼으로 넘어간다.
        return None


def parse_with_form(weekday_kr: str, start: str, end: str,
                    label: str = "보호 시간") -> list[ParsedConstraint]:
    """LLM 없이 쓰는 경로. 이것만으로 시스템 전체가 동작해야 한다."""
    return [ParsedConstraint(
        kind="protected_window", weekday=WEEKDAYS[weekday_kr],
        start=start, end=end, label=label)]


def confirm_and_apply(parsed: list[ParsedConstraint], confirm_fn) -> list[dict]:
    """사용자 확인을 받은 뒤에만 프로필에 반영한다.

    confirm_fn(list[ParsedConstraint]) -> bool
    """
    if not parsed:
        return []
    if not confirm_fn(parsed):
        return []
    return [p.model_dump() for p in parsed]
```

**최소 호출 테스트:**

```powershell
# 위치: C:\dev\fairmeet
python -c "from agent.nl_parser import parse_with_llm; print(parse_with_llm('금요일 저녁 6시부터 10시까지는 알바야'))"
```

기대: `[ParsedConstraint(kind='protected_window', weekday=4, start='18:00', end='22:00', ...)]`
키가 없으면 `None` → **이 경우에도 시스템은 정상 동작해야 합니다.**

**LLM 없이 전체 시스템이 도는지 확인하는 테스트:**

```python
# tests/test_llm_optional.py
import os

def test_system_works_without_llm(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from agent.nl_parser import parse_with_llm, parse_with_form
    assert parse_with_llm("아무 문장") is None
    assert len(parse_with_form("금", "18:00", "22:00")) == 1
```

---

# 16. 단계별 실제 구현 순서

각 단계는 독립적인 체크포인트를 가집니다. **이전 단계의 완료 조건을 만족하기 전에 다음으로 넘어가지 마세요.**

---

### 단계 1 — 공정성 solver (외부 의존성 없음)

- **목표:** 협상의 두뇌를 먼저 만든다. API 없이 100% 테스트 가능
- **선행 조건:** §11.E 가상환경 + `pip install pytest`
- **생성 파일:** `common/fairness.py`, `tests/test_fairness.py`
- **실행 위치:** `C:\dev\fairmeet`
- **실행 명령:** `pytest tests/test_fairness.py -v`
- **기대 결과:** 8개 테스트 전부 통과
- **수동 확인:** `test_balance_flips_the_decision` 이 통과 = 잔액 부호가 맞다
- **대표 오류:** `ModuleNotFoundError: common` → 루트에 빈 `common/__init__.py` 생성
- **완료 조건:** 전체 통과 + 잔액이 zero-sum 유지
- **왜 여기부터인가:** 이 단계가 실패하면 나머지가 전부 무의미합니다. 그리고 API 설정 없이 몇 시간을 벌 수 있습니다

---

### 단계 2 — 베이스라인 비교 실험

- **목표:** "왜 우리 알고리즘이 다른가"를 숫자로 증명. **점수를 만드는 구간**
- **선행 조건:** 단계 1
- **생성 파일:** `eval/compare.py`
- **실행 명령:** `python eval\compare.py`
- **기대 결과:** 4개 전략의 누적 부담과 Gini 비교표
- **완료 조건:** FairMeet의 Gini가 네 전략 중 가장 낮을 것 (§19.2 실측 기준 0.007). 특히 **minimax 단독(0.100)보다 크게 낮아야** 잔액 보정이 실제로 작동한다는 증거가 됩니다

**파일: `eval/compare.py`**

```python
"""4개 전략 비교. 고정 시나리오와 무작위 시나리오를 분리해서 평가한다."""
from __future__ import annotations

import random
import statistics

from common.fairness import (
    build_regret_matrix, gini, solve, solve_first_fit, solve_minimax,
    solve_sum_min, update_balance,
)

STRATEGIES = {
    "선착순(first-fit)": solve_first_fit,
    "합계 최소화(sum-min)": solve_sum_min,
    "minimax": solve_minimax,
    "FairMeet(history-aware)": solve,
}

N_SLOTS = 40
AGENTS = ["A", "B", "C"]


def make_scenario(rng: random.Random, veto_rate=0.25) -> dict[str, list[float | None]]:
    """A는 구조적으로 제약이 많은 사용자 — 돌봄/알바 등을 가정."""
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


def run(strategy, rounds=30, seed=7) -> dict:
    rng = random.Random(seed)
    balance = {a: 0.0 for a in AGENTS}
    cumulative = {a: 0.0 for a in AGENTS}
    failures = 0
    per_round_max = []

    for _ in range(rounds):
        costs = make_scenario(rng)
        try:
            regret, _ = build_regret_matrix(costs)
        except ValueError:
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

    return {
        "cumulative": cumulative,
        "gini": gini(list(cumulative.values())),
        "max_individual": max(cumulative.values()),
        "stdev": statistics.pstdev(list(cumulative.values())),
        "worst_round": max(per_round_max) if per_round_max else None,
        "failures": failures,
    }


def no_common_slot_case():
    """공통 가능 슬롯이 없는 사례 — 모든 전략이 None 을 반환해야 한다."""
    costs = {"A": [None, 5.0], "B": [5.0, None], "C": [5.0, 5.0]}
    regret, _ = build_regret_matrix(costs)
    return {name: fn(regret, {}, {0, 1}) for name, fn in STRATEGIES.items()}


if __name__ == "__main__":
    print(f"{'전략':<26}{'A':>8}{'B':>8}{'C':>8}{'Gini':>8}{'최대':>8}{'실패':>6}")
    print("-" * 72)
    for name, fn in STRATEGIES.items():
        r = run(fn)
        c = r["cumulative"]
        print(f"{name:<26}{c['A']:>8.0f}{c['B']:>8.0f}{c['C']:>8.0f}"
              f"{r['gini']:>8.3f}{r['max_individual']:>8.0f}{r['failures']:>6}")

    print("\n공통 가능 슬롯이 없는 경우:")
    for name, res in no_common_slot_case().items():
        print(f"  {name:<26} → {res}")
    print("\n  (전부 None 이어야 정상. None 처리 경로가 살아 있다는 뜻)")
```

---

### 단계 3 — 슬롯 그리드 + 비용 함수 (수정판)

- **목표:** primary busy를 HARD로 처리하는 올바른 비용 함수
- **생성 파일:** `common/slots.py`, `agent/cost.py`, `agent/profile.py`
- **실행 명령:** `pytest tests/test_cost.py -v`
- **완료 조건:** 기존 일정과 겹치는 슬롯이 반드시 veto로 표시될 것

**파일: `agent/cost.py`** (핵심 수정 사항만)

```python
"""비용 함수. 개인정보에 접근하는 유일한 모듈.

수정 사항:
  - primary 캘린더 busy = HARD (기존 +45 방식 폐기)
  - '이동 가능'은 명시적 flexible 캘린더에만 적용
  - occupied_blocks / busy_minutes (FreeBusy 구간 수는 회의 수가 아님)
  - 자정을 넘는 보호 시간 처리
  - travel_min 제거 (48시간 MVP에서 이동시간 기능 제외)
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

SCALE_VERSION = "cost-rules-v1"      # 모든 에이전트가 동일해야 함
MAX_COST = 100.0


@dataclass
class Profile:
    agent_id: str
    hard_calendars: list[str] = field(default_factory=list)
    primary_calendars: list[str] = field(default_factory=lambda: ["primary"])
    flexible_calendars: list[str] = field(default_factory=list)
    protected_windows: list[tuple] = field(default_factory=list)  # (wd,"HH:MM","HH:MM")
    chronotype: str = "neutral"
    soft_busy_minutes_budget: int = 240      # 하루 이 분 이상 차 있으면 가산
    mode: str = "work"


def _overlap(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def _protected_hit(slot_start, slot_end, wd, t0, t1) -> bool:
    """자정을 넘는 보호 시간(예: 22:00~06:00)도 올바르게 처리한다."""
    h0, m0 = map(int, t0.split(":"))
    h1, m1 = map(int, t1.split(":"))
    day = slot_start.date()
    p0 = dt.datetime.combine(day, dt.time(h0, m0), tzinfo=slot_start.tzinfo)
    p1 = dt.datetime.combine(day, dt.time(h1, m1), tzinfo=slot_start.tzinfo)
    if p1 <= p0:                                   # 자정을 넘김
        p1 += dt.timedelta(days=1)
        if slot_start.weekday() == (wd + 1) % 7:   # 다음날 새벽 구간도 검사
            p0 -= dt.timedelta(days=1)
            p1 -= dt.timedelta(days=1)
        elif slot_start.weekday() != wd:
            return False
    elif slot_start.weekday() != wd:
        return False
    return _overlap(slot_start, slot_end, p0, p1)


def _chronotype_penalty(hour: int, ctype: str) -> float:
    if ctype == "morning":
        return {9: 0, 10: 0, 11: 5, 12: 20, 13: 15, 14: 10,
                15: 18, 16: 25, 17: 32, 18: 40}.get(hour, 45)
    if ctype == "evening":
        return {9: 40, 10: 32, 11: 22, 12: 20, 13: 8, 14: 5,
                15: 0, 16: 0, 17: 5, 18: 10}.get(hour, 15)
    return {12: 18, 13: 12}.get(hour, 6)


def cost_vector(profile: Profile, slots, busy_by_cal: dict[str, list[tuple]]):
    """Returns: (costs, veto_mask) — costs[i] 는 veto 면 None."""
    costs: list[float | None] = []
    veto: list[bool] = []

    # 일별 점유 시간 (구간 수가 아니라 분 단위로 측정)
    busy_minutes_by_day: dict[dt.date, int] = {}
    for cal in profile.primary_calendars:
        for b0, b1 in busy_by_cal.get(cal, []):
            m = int((b1 - b0).total_seconds() // 60)
            busy_minutes_by_day[b0.date()] = busy_minutes_by_day.get(b0.date(), 0) + m

    for (s, e) in slots:
        blocked = False

        # 1) HARD 캘린더 (돌봄·통원 등) → 거부권. 제목은 모른다
        for cal in profile.hard_calendars:
            if any(_overlap(s, e, b0, b1) for b0, b1 in busy_by_cal.get(cal, [])):
                blocked = True
                break

        # 2) primary 캘린더의 기존 일정 → 원칙적으로 거부권
        #    (단, flexible 캘린더에 같은 구간이 있으면 이동 가능으로 간주)
        if not blocked:
            for cal in profile.primary_calendars:
                if any(_overlap(s, e, b0, b1) for b0, b1 in busy_by_cal.get(cal, [])):
                    movable = any(
                        _overlap(s, e, f0, f1)
                        for fc in profile.flexible_calendars
                        for f0, f1 in busy_by_cal.get(fc, []))
                    if not movable:
                        blocked = True
                    break

        # 3) 사용자 선언 보호 시간 → 거부권
        if not blocked:
            for wd, t0, t1 in profile.protected_windows:
                if _protected_hit(s, e, wd, t0, t1):
                    blocked = True
                    break

        # 4) 업무 모드의 근무시간 밖
        if not blocked and profile.mode == "work":
            if s.hour < 8 or e.hour > 20 or s.weekday() >= 5:
                blocked = True

        # 5) 늦은 밤은 모드 무관 거부권
        if not blocked and (e.hour >= 23 or s.hour < 7):
            blocked = True

        if blocked:
            costs.append(None)
            veto.append(True)
            continue

        c = 0.0
        # 이동 가능한 일정을 밀어내는 비용
        if any(_overlap(s, e, f0, f1)
               for fc in profile.flexible_calendars
               for f0, f1 in busy_by_cal.get(fc, [])):
            c += 35.0
        c += _chronotype_penalty(s.hour, profile.chronotype)

        # 그날 이미 얼마나 차 있는지 (분 단위)
        over = busy_minutes_by_day.get(s.date(), 0) - profile.soft_busy_minutes_budget
        if over > 0:
            c += min(25.0, over / 12.0)

        # 앞뒤 90분이 비어 있으면 하루를 쪼개는 회의
        near = any(_overlap(s - dt.timedelta(minutes=90),
                            e + dt.timedelta(minutes=90), b0, b1)
                   for cal in profile.primary_calendars
                   for b0, b1 in busy_by_cal.get(cal, []))
        if not near:
            c += 10.0

        costs.append(min(MAX_COST, c))
        veto.append(False)

    return costs, veto
```

---

### 단계 4 — OAuth + FreeBusy

- **목표:** 실제 Google 계정 3개의 busy 구간을 읽어온다
- **선행 조건:** §13.1의 1~7단계 완료
- **생성 파일:** `agent/calendar_client.py`, `scripts/bootstrap_tokens.py`
- **실행 명령:** `python scripts\bootstrap_tokens.py a b c` → `pytest -m live -k freebusy`
- **기대 결과:** `tokens/token_{a,b,c}.json` 생성, busy 구간 출력
- **대표 오류:** §20의 OAuth 항목 참조
- **완료 조건:** 3개 계정 전부 조회 성공. `git status`에 `tokens/`가 없을 것

---

### 단계 5 — DB + 상태 머신

- **생성 파일:** `mediator/schema.sql`, `mediator/store.py`, `mediator/payload_guard.py`
- **실행 명령:** `python -c "from mediator.store import init_db; init_db()"` → `pytest tests/test_store.py -v`
- **완료 조건:** 같은 전이를 두 번 호출하면 두 번째가 `False`를 반환할 것 (멱등성)

---

### 단계 6 — Agent 서버 2개

- **목표:** 2인 협상부터. 3인은 나중에
- **생성 파일:** `agent/server.py`, `profiles/a.json`, `profiles/b.json`
- **실행 명령 (PowerShell — 터미널마다):**

```powershell
# 터미널 A
$env:FAIRMEET_PROFILE = "profiles\a.json"
$env:FAIRMEET_TOKEN   = "tokens\token_a.json"
uvicorn agent.server:app --port 8001
```

```powershell
# 터미널 B
$env:FAIRMEET_PROFILE = "profiles\b.json"
$env:FAIRMEET_TOKEN   = "tokens\token_b.json"
uvicorn agent.server:app --port 8002
```

> ⚠️ PowerShell에서는 `VAR=x command` 형식이 동작하지 않습니다. `$env:VAR = "x"` 를 **별도 줄에** 쓰세요.

- **수동 확인:**

```powershell
curl.exe http://127.0.0.1:8001/.well-known/agent-card.json
```

- **완료 조건:** 두 에이전트가 모두 Agent Card를 반환할 것

---

### 단계 7 — Mediator 협상 루프

- **생성 파일:** `mediator/negotiation.py`, `mediator/server.py`
- **완료 조건:** 협상 로그가 콘솔에 출력되고, `counter_indices`가 다음 라운드 후보를 좁히는 것이 로그에서 보일 것

---

### 단계 8 — 웹 승인

- **생성 파일:** `mediator/approval_web.py`
- **완료 조건:** 참가자별 링크가 각각 발급되고, 같은 링크를 두 번 열면 "이미 응답하셨습니다"가 나올 것

---

### 단계 9 — 재확인 + 멱등 커밋

- **생성 파일:** `mediator/commit.py`
- **수동 확인:** 승인 직후 다른 브라우저에서 해당 시간에 일정을 **일부러 만든 뒤** 승인 완료 → `STALE_CONFLICT`가 나오는지
- **완료 조건:** 실제 캘린더에 이벤트가 **정확히 하나** 생성될 것

---

### 단계 10 — 대시보드

- **생성 파일:** `dashboard/index.html` (SSE로 협상 로그 + 잔액 막대)
- **완료 조건:** 협상 과정이 화면에서 실시간으로 흐를 것

---

### 단계 11 — 3인 확장 + 리허설

- 에이전트 C 추가, 전체 흐름 3회 반복 실행
- 네트워크 없는 환경 대비 **녹화본** 준비

---

# 17. 전체 서비스 실행 · 종료 · 점검

## 17.1 실행 (Docker 없이 — 기본 경로)

VS Code 통합 터미널을 4개 엽니다 (`` Ctrl+Shift+` `` 로 추가).

| 터미널 | 명령 | 확인 URL |
|---|---|---|
| 1 | `uvicorn mediator.server:app --port 8000 --reload` | http://127.0.0.1:8000/health |
| 2 | `$env:FAIRMEET_PROFILE="profiles\a.json"; $env:FAIRMEET_TOKEN="tokens\token_a.json"` 뒤 새 줄에 `uvicorn agent.server:app --port 8001` | http://127.0.0.1:8001/health |
| 3 | 같은 방식, `b`, 포트 8002 | http://127.0.0.1:8002/health |
| 4 | 같은 방식, `c`, 포트 8003 | http://127.0.0.1:8003/health |

각 터미널에서 먼저 `.\.venv\Scripts\Activate.ps1` 을 실행해야 합니다.

## 17.2 로그에서 확인할 항목

| 로그 | 의미 |
|---|---|
| `cost_collected agents=3 scale=cost-rules-v1` | 전원 비용 수신, 규칙 버전 일치 |
| `propose round=1 slot=17 max_adj=23.4` | 제안 발생 |
| `counter received indices=[22,23]` | 역제안이 실제로 오고 있음 |
| `transition PENDING_HUMAN_APPROVAL -> RECHECKING_CALENDARS` | 전원 승인됨 |
| `commit event=fm8f2a... created=True` | 이벤트 생성 성공 |
| `balance applied session=...` | 잔액 갱신 (커밋 후 1회) |
| ⚠️ `CalendarUnavailable` | fail-closed 발동. 빈 시간으로 처리하지 않았음 |

## 17.3 종료

각 터미널에서 `Ctrl+C`. 포트가 남으면:

```powershell
Get-NetTCPConnection -LocalPort 8000,8001,8002,8003 -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique |
  ForEach-Object { Stop-Process -Id $_ -Force }
```

## 17.4 발표 전 preflight 스크립트

**파일: `scripts/preflight.ps1`**

```powershell
# 발표 당일 아침에 반드시 실행. 위치: C:\dev\fairmeet
$ErrorActionPreference = "Stop"
$fail = 0

function Check($name, $block) {
    Write-Host -NoNewline ("{0,-46}" -f $name)
    try {
        & $block
        Write-Host "OK" -ForegroundColor Green
    } catch {
        Write-Host "FAIL" -ForegroundColor Red
        Write-Host "   $_" -ForegroundColor DarkRed
        $script:fail++
    }
}

Check "가상환경 활성" { if (-not $env:VIRTUAL_ENV) { throw "activate 먼저" } }
Check "의존성 설치" { python -c "import fastapi, googleapiclient" }
Check ".env 로드" { python -c "from common.config import GROUP_SECRET" }
Check "단위 테스트" {
    $o = pytest -q -m "not live" 2>&1
    if ($LASTEXITCODE -ne 0) { throw ($o | Select-Object -Last 5) }
}
foreach ($u in @("a","b","c")) {
    Check "토큰 유효: $u" {
        python -c @"
import datetime as dt
from zoneinfo import ZoneInfo
from agent.calendar_client import get_service, fetch_busy
s = get_service('tokens/token_$u.json')
n = dt.datetime.now(ZoneInfo('Asia/Seoul'))
fetch_busy(s, ['primary'], n, n + dt.timedelta(days=7))
"@
    }
}
Check "DB 접근" { python -c "from mediator.store import init_db; init_db()" }
Check "베이스라인 실험" { python eval\compare.py | Out-Null }

Write-Host ""
if ($fail -eq 0) {
    Write-Host "전체 통과 — 시연 준비 완료" -ForegroundColor Green
} else {
    Write-Host "$fail 개 실패 — 시연 전에 해결하세요" -ForegroundColor Red
    Write-Host "토큰 실패라면: python scripts\bootstrap_tokens.py a b c" -ForegroundColor Yellow
    exit 1
}
```

## 17.5 Docker (선택)

Docker는 **필요하지 않습니다.** 쓰더라도 비밀 파일을 이미지에 복사하지 마세요:

```yaml
# docker-compose.yml — 참고용
services:
  mediator:
    build: .
    ports: ["8000:8000"]
    volumes:
      - ./tokens:/app/tokens:ro          # read-only 볼륨
      - ./credentials.json:/app/credentials.json:ro
    env_file: .env                        # 이미지에 COPY 하지 않음
    command: uvicorn mediator.server:app --host 0.0.0.0 --port 8000
```

`.dockerignore`에 `.env`, `credentials.json`, `tokens/`를 반드시 넣으세요.

---

# 18. 24시간 · 48시간 · 1주 버전의 범위

| 기능 | 24h | 48h (권장) | 1주 |
|---|:---:|:---:|:---:|
| 공정성 solver + 잔액 | ✅ | ✅ | ✅ |
| 베이스라인 비교 실험 | ✅ | ✅ | ✅ |
| 비용 함수 (HARD 처리) | ✅ | ✅ | ✅ |
| OAuth + FreeBusy 실제 조회 | ✅ | ✅ | ✅ |
| **실제 이벤트 생성 (멱등)** | ✅ | ✅ | ✅ |
| 참가자 수 | 2인 | 3인 | 5인 |
| 상태 머신 + DB | 간소화 (메모리) | ✅ SQLite | ✅ |
| 승인 UI | CLI 확인 | ✅ 웹 개별 링크 | 웹 + Slack |
| 커밋 직전 재확인 | ❌ | ✅ | ✅ |
| 역제안(`counter_indices`) 반영 | ❌ | ✅ | ✅ |
| 대시보드 | ❌ | ✅ SSE | ✅ |
| payload 허용 필드 검증 | ❌ | ✅ | ✅ |
| HMAC 가명 / nonce | ❌ | 가명만 | ✅ 둘 다 |
| Slack | ❌ | ❌ | ✅ |
| LLM 제약 파서 | ❌ | ❌ | ✅ |
| 이동시간 (Routes API) | ❌ | ❌ | 검토 |
| A2A 1.0 SDK 전환 | ❌ | ❌ | 검토 |
| progressive-disclosure | ❌ | ❌ | 시뮬레이션만 |

**24시간 버전의 최소 시연:** "두 사람의 실제 캘린더 → 협상 → 터미널에서 승인 → 실제 이벤트 생성" + "베이스라인 비교표". 이것만으로도 핵심 주장은 전부 증명됩니다.

---

# 19. 자동 테스트 및 발표 리허설 계획

## 19.1 테스트 목록

| # | 테스트 | 검증 대상 | 파일 |
|---|---|---|---|
| T1 | regret이 자기 최솟값 기준 상대값 | 일괄 부풀림 무효화 | `test_fairness.py` |
| T2 | 잔액이 결정을 뒤집음 | 부호 정합성 | `test_fairness.py` |
| T3 | 잔액 zero-sum + clamp | 수치 안정성 | `test_fairness.py` |
| T4 | veto 슬롯 절대 미선택 | **HARD 위반 0건** | `test_fairness.py` |
| T5 | 공통 슬롯 없음 → `None` | 실패 경로 | `test_fairness.py` |
| T6 | 민감 필드 전송 시 400 | 허용 필드 스키마 | `test_payload_guard.py` |
| T7 | `events().list/get` 미호출 | 애플리케이션 수준 통제 | `test_no_event_read.py` |
| T8 | 같은 session_id 2회 insert → 이벤트 1개 | **중복 생성 0건** | `test_calendar_smoke.py` |
| T9 | 전이 2회 호출 → 2번째 `False` | 멱등성 | `test_store.py` |
| T10 | 승인 중복 클릭 → `duplicate` | 멱등성 | `test_store.py` |
| T11 | TTL 초과 → `EXPIRED` | 만료 처리 | `test_store.py` |
| T12 | 커밋 전 충돌 → `STALE_CONFLICT`, 이벤트 미생성 | **stale commit 0건** | `test_commit.py` |
| T13 | 커밋 실패 시 잔액 미갱신 | 원장 무결성 | `test_commit.py` |
| T14 | FreeBusy 실패 → `CalendarUnavailable` 전파 | **fail-closed** | `test_commit.py` |
| T15 | 에이전트 타임아웃 → `AGENT_UNAVAILABLE` | 장애 처리 | `test_negotiation.py` |
| T16 | LLM 없이 전체 동작 | fallback | `test_llm_optional.py` |
| T17 | 4개 전략 동일 데이터 비교 | 평가 타당성 | `eval/compare.py` |

## 19.2 지표 — 근거 없는 목표치는 쓰지 않음

| 지표 | 측정 방법 | 목표 |
|---|---|---|
| HARD 위반 | T4 + 실행 로그 | **0건 (하드 요구사항)** |
| 중복 이벤트 | T8 + 캘린더 육안 확인 | **0건 (하드 요구사항)** |
| stale commit | T12 | **0건 (하드 요구사항)** |
| 민감 필드 전송 | T6 | **0건 (하드 요구사항)** |
| 누적 regret Gini | `eval/compare.py` 30라운드 | **전 베이스라인 대비 최저** (아래 실측 참조) |
| 누적 부담 분산 | 같음 | first-fit·sum-min·minimax 대비 **감소** |
| 합의 성공률 | 협상 로그 | 측정하되 목표치는 **초기 가설**로만 |
| 평균 라운드 수 | 협상 로그 | 측정만 |
| 인간 재협상률 | 승인 로그 | 측정만 |

> **이전 초안의 `Gini < 0.15`, `escalation 10~20%` 는 근거가 없으므로 삭제했습니다.** 발표에서는 "베이스라인 대비 상대적 개선"만 주장하고, 절대 수치를 목표로 제시하지 마세요.

### 실측 결과 (`eval/compare.py`, seed=7, 30라운드, A가 구조적으로 제약이 많은 시나리오)

이 문서의 코드를 그대로 실행한 결과입니다. 시나리오가 무작위 생성이므로 seed를 바꾸면 값이 달라집니다.

| 전략 | A 누적 | B 누적 | C 누적 | Gini | 최대 개인 누적 |
|---|---:|---:|---:|---:|---:|
| 선착순(first-fit) | 893 | 964 | 947 | 0.017 | 964 |
| 합계 최소화(sum-min) | 363 | 425 | 449 | 0.046 | **449** |
| minimax (잔액 없음) | 350 | 443 | 551 | 0.100 | 551 |
| **FairMeet** | 469 | 459 | 473 | **0.007** | 473 |

**이 표를 정직하게 읽는 법 — 발표 전에 반드시 이해하고 가세요:**

1. **FairMeet의 Gini가 가장 낮습니다 (0.007).** 세 사람의 누적 부담이 469/459/473으로 거의 동일합니다. 이것이 이 프로젝트가 실제로 달성한 것입니다.
2. **FairMeet은 총 부담 최소화에서는 sum-min에 집니다** (1401 vs 1237). 당연합니다 — 총합이 아니라 분배를 최적화하니까요. **"우리가 모든 면에서 낫다"고 말하지 마세요.** 트레이드오프를 먼저 인정하는 편이 강합니다.
3. **최대 개인 누적도 sum-min(449)이 FairMeet(473)보다 낮습니다.** 이 시나리오에서는 그렇습니다. FairMeet의 장점은 "최댓값이 가장 낮다"가 아니라 **"세 사람의 값이 서로 가장 가깝다"** 입니다. 지표 이름을 정확히 쓰세요.
4. **minimax 단독은 오히려 가장 불평등합니다 (0.100).** 매 라운드 최댓값만 보면 구조적으로 여유 있는 사람이 반복해서 손해를 봅니다. **잔액 보정을 더했을 때만 0.100 → 0.007로 떨어집니다.** 이것이 `concession_balance`의 존재 이유를 증명하는 가장 강력한 한 줄입니다.
5. first-fit은 Gini가 낮아 보이지만(0.017) **절대 부담이 2배**입니다(964 vs 473). "공평하게 모두가 불행한" 해입니다. Gini만 보고 판단하면 안 되는 이유이므로, 절대값과 함께 제시하세요.

**발표에서 쓸 한 문장:**

> "순수 minimax는 Gini 0.100, 여기에 누적 양보 보정을 더하면 0.007입니다. 총 부담은 sum-min이 더 낮지만, 그 해는 특정 참여자에게 부담이 쏠립니다. 저희가 최적화한 것은 효율이 아니라 분배입니다."

## 19.3 사용자 테스트 (팀 외부 5명)

4개 문항만 물으면 충분합니다:

1. 결과로 나온 시간에 만족하셨습니까? (1–5)
2. **이유를 설명하지 않고 거절할 수 있다는 점**이 유용했습니까? (1–5)
3. 이 시스템이 내 일정 정보를 적절히 다룬다고 느끼셨습니까? (1–5)
4. 기존 방식(단톡방 조율) 대비 시간이 절약되었습니까? (1–5)

2번이 핵심 가설의 직접 검증입니다.

## 19.4 장애 주입 테스트 (리허설 전날)

| 시나리오 | 주입 방법 | 기대 동작 |
|---|---|---|
| Agent 다운 | 터미널 하나를 `Ctrl+C` | `AGENT_UNAVAILABLE`, 크래시 없음 |
| FreeBusy 오류 | 토큰 파일을 잠시 이름 변경 | `CalendarUnavailable` 전파, **빈 시간 처리 안 함** |
| 승인 중복 클릭 | 링크를 3번 연속 클릭 | 1회만 기록 |
| 커밋 중 충돌 | 승인 대기 중 해당 시간에 일정 생성 | `STALE_CONFLICT` |
| 네트워크 끊김 | Wi-Fi 끄기 | 명확한 오류 메시지, 녹화본으로 대체 |

## 19.5 리허설 (3회)

| 회차 | 초점 |
|---|---|
| 1회 | 전체 흐름이 끊기지 않는지. 시간 측정 |
| 2회 | 장애 주입 후 복구. "만약 안 되면" 대사 준비 |
| 3회 | 발표 대본 확정 + **화면 녹화** (Wi-Fi 실패 대비) |

**시연 대본 (5분):**

1. (30초) 문제 — "거절하려면 이유를 말해야 한다"
2. (60초) 3개 캘린더 + `/meet` 실행 → 협상 로그 흐름
3. (60초) 잔액 원장 화면 — "C가 지난 2회 연속 양보했습니다"
4. (60초) 개별 승인 링크 → 전원 승인 → **실제 캘린더에 이벤트가 꽂히는 순간**
5. (60초) 베이스라인 비교표 — Gini 차이
6. (30초) 한계 정직하게 — "비용 벡터도 메타데이터입니다. 다음 단계는 progressive disclosure입니다"

6번을 **먼저 말하는 것**이 심사에서 유리합니다.

---

# 20. 단계별 오류 해결표

| 오류 | 원인 | 확인법 | 해결 |
|---|---|---|---|
| `python`을 찾을 수 없음 / Store가 열림 | 설치 시 "Add to PATH" 미체크 | `where.exe python` | Python 재설치(PATH 체크). 또는 설정 → 앱 → 앱 실행 별칭 → python.exe 끄기 |
| `claude`를 찾을 수 없음 | CLI 미설치 (확장만 설치됨) | `where.exe claude` | 확장은 자체 CLI를 번들하므로 채팅 패널은 동작. 터미널에서 쓰려면 `irm https://claude.ai/install.ps1 \| iex` |
| `이 시스템에서 스크립트를 실행할 수 없으므로` | PowerShell 실행 정책 | `Get-ExecutionPolicy -Scope CurrentUser` | `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |
| VS Code가 다른 Python 사용 | 인터프리터 미선택 | 상태바 좌하단 확인 | `Ctrl+Shift+P` → Python: Select Interpreter → `.venv` |
| Claude Code 로그인 창 안 열림 | 확장 상태 이상 | — | `Ctrl+Shift+P` → Developer: Reload Window. 그래도 안 되면 `Ctrl+Shift+P` → Claude Code: Logout 후 재시도 |
| OAuth `redirect_uri_mismatch` | 웹 앱 유형으로 클라이언트 생성 | GCP → 사용자 인증 정보 → 유형 확인 | **데스크톱 앱** 유형으로 새로 만들고 `credentials.json` 교체 |
| OAuth `access_denied` | 테스트 사용자 미등록 | OAuth 동의 화면 → 테스트 사용자 | 해당 Gmail 추가 후 재시도 |
| `이 앱은 확인되지 않았습니다` | 테스트 모드 정상 동작 | — | 고급 → "FairMeet(으)로 이동" 클릭 |
| `invalid_grant` / refresh token 만료 | **테스트 모드 외부 앱은 7일 만료** | 토큰 파일 수정 시각 | `tokens\token_*.json` 삭제 후 `python scripts\bootstrap_tokens.py a b c` |
| Calendar `401` | access token 만료, 갱신 실패 | 로그의 `CalendarUnavailable` | 위와 동일하게 재발급 |
| Calendar `403 insufficientPermissions` | 스코프 부족 | 토큰 JSON의 `scopes` 확인 | 토큰 삭제 후 올바른 SCOPES로 재발급 |
| FreeBusy `notFound` | 캘린더 ID 오타 또는 미공유 | 응답의 `errors` 배열 | `primary` 사용. 보조 캘린더는 해당 계정 소유인지 확인 |
| **이벤트 중복 생성** | 사용자별로 insert 호출 | 캘린더 육안 확인 | organizer 1명만 insert. `make_event_id()` 멱등성 키 사용 |
| **커밋 직전 충돌** | 승인 대기 중 일정 추가됨 | `STALE_CONFLICT` 로그 | 정상 동작. 슬롯 제외 후 재협상 |
| Slack `dispatch_failed` | 3초 내 미응답 또는 URL 오류 | 서버 로그 | `ack()`를 핸들러 첫 줄로. 무거운 작업은 별도 스레드 |
| Slack `invalid_auth` | 봇 토큰 오류/미설치 | `.env`의 `SLACK_BOT_TOKEN` | Install to Workspace 재실행, `xoxb-` 토큰 재복사 |
| Slack 서명 검증 실패 | Signing Secret 불일치 | Basic Information | 재복사. `App(signing_secret=...)` 전달 확인 |
| 터널 URL 변경 | ngrok 재시작 | ngrok 콘솔 | Slash Commands + Interactivity **두 곳 모두** 수정 |
| 포트 충돌 (`address already in use`) | 이전 프로세스 잔존 | `Get-NetTCPConnection -LocalPort 8001` | §17.3 종료 명령 실행 |
| Agent timeout | 에이전트 다운/응답 지연 | Mediator 로그 | `httpx` timeout 10초, 실패 시 `AGENT_UNAVAILABLE` |
| **공통 가능 슬롯 없음** | 전원 거부권 겹침 | `NO_FEASIBLE_SLOT` | 정상 동작. 사람에게 에스컬레이션. 윈도우를 2주로 확대 제안 |
| `ModuleNotFoundError: common` | 패키지 초기화 누락 | — | 각 폴더에 빈 `__init__.py` 생성. 루트에서 실행 |
| SQLite `database is locked` | 동시 쓰기 | — | `PRAGMA journal_mode=WAL` 확인, `timeout=10.0` |

---

# 21. 심사위원에게 해도 되는 주장 / 하면 안 되는 주장

## ✅ 해도 되는 주장

- "**조회에는 FreeBusy 스코프만 사용**하며, 이 API는 일정 제목을 반환하지 않습니다."
- "애플리케이션 코드에서 `events().list`/`get`을 **한 번도 호출하지 않으며, 이를 정적 테스트로 검증**합니다."
- "중재자가 받는 payload는 **허용 필드 스키마로 강제**되어 있으며, 제목·설명·위치·사유 필드는 스키마에 존재하지 않습니다."
- "목적함수가 총합 효율이 아니라 **최대 개인 불이익의 최소화 + 누적 부담 보정**입니다."
- "regret을 각자의 최소 비용 기준 상대값으로 정규화하기 때문에, **비용을 일괄 부풀리는 전략은 구조적으로 무효화**됩니다."
- "확정은 **전원 승인 없이는 일어나지 않으며**, 승인 여부는 공개 채널에 노출되지 않습니다."
- "커밋 직전 전원 캘린더를 재확인하고, **세션 ID 기반 멱등성 키**로 중복 생성을 방지합니다."
- "FreeBusy 오류를 **빈 시간으로 해석하지 않고 fail-closed** 처리합니다."
- "A2A **개념을 차용했으나 1.0 규격 준수는 아니며**, 전환 계획은 문서화되어 있습니다."

## ❌ 하면 안 되는 주장

| 금지 | 이유 |
|---|---|
| "완전 탈중앙 협상" | 중앙 중재자가 전체 비용 벡터를 받습니다 |
| "제로 지식" / "프라이버시 보장" / "유출 불가능" | 비용 벡터는 생활 패턴 추론이 가능한 메타데이터입니다 |
| "API 권한 수준에서 제목을 읽을 수 없다" | `calendar.events.owned`도 소유 캘린더 이벤트를 읽을 수 있습니다 |
| "A2A 호환" / "표준 준수" | Task 생명주기·보안 스킴·버전 협상 미구현 |
| "privacy leak = 0" | 측정 불가능한 주장입니다 |
| "권력 구조를 제거한다" | 의사결정 보조 장치일 뿐입니다 |
| "Gini 0.15 이하 달성" | 근거 없는 목표치입니다 |
| "기존 제품이 구조적으로 불가능한 일" | 차이는 목적함수와 노출 최소화 설계입니다 |
| "이동 최적화로 탄소를 줄인다" | 이동시간 기능을 제외했습니다 |

## 예상 질문과 답변

**Q. 중앙 서버가 전부 보는데 왜 에이전트인가요?**
> 맞습니다. MVP는 완전 탈중앙이 아니라 연합형 효용 계산 구조이고, 중재자는 비용 벡터를 받습니다. 에이전트 분리의 실익은 두 가지입니다. 첫째, 원본 캘린더 내용과 개인 사유가 프로세스 경계를 넘지 않습니다. 둘째, 다음 단계인 progressive disclosure로 전환할 때 중재자만 바꾸면 되도록 설계했습니다. 현재 구조의 한계는 문서 §5.1에 명시해 두었습니다.

**Q. Reclaim이나 Clockwise랑 뭐가 다른가요?**
> 목적함수가 다릅니다. 그 제품들은 총 효율을 최적화합니다. 저희는 가장 불리한 사람의 불이익을 최소화하고, 과거 누적 부담을 다음 결정에 반영합니다. §2의 비교 실험에서 같은 데이터로 네 전략을 돌린 결과를 보여드릴 수 있습니다.

**Q. 사용자가 거짓말하면요?**
> 일괄 부풀림은 regret 정규화로 자동 무효화됩니다. 하지만 특정 슬롯만 과장하는 공격은 막지 못합니다. 이건 저희 시스템의 알려진 한계이고, 비용 규칙 버전 검증과 범위 검증으로 완화만 했습니다.

**Q. 비용 벡터로 뭘 알 수 있나요?**
> 정직하게 말씀드리면, 꽤 많습니다. 매주 같은 시간이 거부권이면 정기 일정의 존재가 드러나고, 평일 오전이 전부 고비용이면 근무 형태를 짐작할 수 있습니다. 제목은 감췄지만 형태는 감추지 못했습니다. 그래서 "프라이버시 보장"이 아니라 "노출 최소화"라고 말합니다.

---

# 22. 최종 Go/No-Go 체크리스트

## 22.1 현재 초안을 그대로 구현해도 되는가

**아니오.** §2의 P0 7건을 고치지 않으면 다음이 발생합니다: 이벤트가 3중으로 생성되고, Slack 승인 코드가 `NameError`로 죽고, 공정성 기준이 라운드마다 흔들리고, 기존 일정 위에 회의가 겹치고, 발표 당일 토큰이 만료됩니다.

## 22.2 무엇을 삭제해야 하는가

**코드에서:** 이동시간(`travel_min`), 사용자별 다중 `events.insert`, `+45` 소프트 충돌, `max_meetings_per_day`, 라운드마다 재계산되는 regret 기준선.

**주장에서:** "완전 탈중앙", "제로 지식", "API 권한 수준 보호", "A2A 호환", "privacy leak = 0", "권력 구조 제거", "Gini < 0.15", "escalation 10~20%".

**48시간 범위에서:** Slack, LLM 파서, Routes API, A2A SDK, 4인 이상 협상, 반복 일정, 자체 캘린더 UI.

## 22.3 무엇을 먼저 구현해야 하는가

1. **공정성 solver + 단위 테스트** (API 불필요, 점수의 핵심)
2. **베이스라인 비교 실험** (차별점의 증거)
3. **OAuth + FreeBusy** (가장 잘 터지는 지점을 일찍)
4. **실제 이벤트 멱등 생성** ("실제 시연"의 유일한 증거)
5. 상태 머신 + 웹 승인
6. 재확인 + 커밋 오케스트레이션
7. 대시보드

## 22.4 48시간 내 안정적으로 시연 가능한 최소 범위

> **3인의 실제 Google 계정 → FreeBusy 조회 → 로컬 비용 산출 → 중재자 공정 최적화(잔액 반영) → 개별 승인 링크 → 전원 승인 → 캘린더 재확인 → organizer 캘린더에 이벤트 1개 생성**
>
> **+ 4개 전략 비교표 (Gini / 최대 개인 부담)**
> **+ 잔액 원장 시각화**

이 범위는 §16의 단계 1~9에 해당하며, 각 단계가 독립 체크포인트를 가지므로 어디서 시간이 부족해도 그 지점까지는 시연할 수 있습니다.

## 22.5 시연 전날 점검

- [ ] `python scripts\bootstrap_tokens.py a b c` **재실행** (refresh token 7일 만료)
- [ ] `pytest -q -m "not live"` 전체 통과
- [ ] `pytest -q -m live` 통과 (FreeBusy + 멱등성)
- [ ] 시연용 캘린더에 **그럴듯한 일정을 미리 채움** (빈 캘린더면 협상이 싱겁게 끝남)
- [ ] HARD 캘린더(돌봄/통원/알바)를 실제로 만들어 **거부권 발동 장면** 준비
- [ ] 잔액 원장에 **과거 이력 2~3건 주입** (부채 보정이 눈에 보이게)
- [ ] `python eval\compare.py` 출력을 슬라이드에 반영
- [ ] 전체 흐름 **녹화본** 저장 (Wi-Fi 실패 대비)
- [ ] `git status`에 `.env` / `tokens/` / `credentials.json` 없음 확인
- [ ] 발표 슬라이드와 화면에 **API 키·토큰이 찍히지 않는지** 확인
- [ ] 장애 주입 테스트 5종 (§19.4) 통과

## 22.6 시연 당일 아침

- [ ] `.\scripts\preflight.ps1` → **전체 통과**
- [ ] 4개 터미널 기동, 각 `/health` 응답 확인
- [ ] 브라우저에 승인 페이지 미리 로드
- [ ] 캘린더 3개를 화면에 배치
- [ ] 이전 테스트 이벤트 삭제 (시연 중 혼동 방지)
- [ ] 발표장 네트워크에서 Google API 접근 가능한지 확인 (일부 대학 네트워크는 차단)
- [ ] 노트북 절전 모드 해제, 알림 끄기

---

## 부록 A. 확인이 필요한 항목

| 항목 | 상태 |
|---|---|
| `calendar.events.owned` 스코프만으로 `conferenceData` Meet 링크 생성이 되는지 | **⚠️ 확인 필요.** 안 되면 `calendar.events`로 넓혀야 하며, 그 경우 §21의 주장을 더 보수적으로 조정할 것 |
| 커스텀 event ID 길이·문자 규칙의 현재 정확한 범위 | **⚠️ 확인 필요.** 본 문서는 base32hex(a-v, 0-9), 5~1024자를 전제. `409` 응답으로 멱등성이 동작하는지 T8로 반드시 검증할 것 |
| ngrok 무료 플랜의 현재 제한 | **⚠️ 확인 필요** |
| Anthropic API 모델명 (`claude-sonnet-4-5`) 및 가격 | **⚠️ 확인 필요.** [공식 가격 페이지](https://www.anthropic.com/pricing) |
| `requirements.txt` 각 패키지의 최신 호환 버전 | **⚠️ 확인 필요** |

## 부록 B. 공식 출처

- [Google Calendar API 스코프](https://developers.google.com/workspace/calendar/api/auth)
- [Google Calendar FreeBusy: query](https://developers.google.com/workspace/calendar/api/v3/reference/freebusy/query)
- [Google Calendar 이벤트 생성](https://developers.google.com/workspace/calendar/api/guides/create-events)
- [Google OAuth 2.0 (토큰 만료 규칙 포함)](https://developers.google.com/identity/protocols/oauth2)
- [A2A 1.0 명세](https://a2a-protocol.org/latest/specification/)
- [Slack Bolt for Python — 요청 승인(ack)](https://docs.slack.dev/tools/bolt-python/concepts/acknowledge/)
- [Claude Code 설치와 초기 설정](https://code.claude.com/docs/en/setup)
- [VS Code에서 Claude Code 사용](https://code.claude.com/docs/en/vs-code)
- [Google Routes — Compute Route Matrix](https://developers.google.com/maps/documentation/routes/compute_route_matrix) (MVP 제외, 참고용)
