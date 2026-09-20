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
    regret_json       TEXT,            -- {agent_id: [r|null, ...]}  계산 후 불변
    candidate_mask    TEXT,            -- JSON 정수 배열 (단조 감소)
    preferred_set     TEXT,            -- JSON 정수 배열 (역제안 누적)
    round             INTEGER NOT NULL DEFAULT 0,
    proposed_slot     INTEGER,
    proposed_regrets  TEXT,            -- {agent_id: regret}
    google_event_id   TEXT,
    balance_applied   INTEGER NOT NULL DEFAULT 0,   -- 0/1 잔액 중복 갱신 방지
    fail_reason       TEXT,
    expires_at        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    proposal_version  INTEGER NOT NULL DEFAULT 0,   -- 현재 사람 승인 라운드(제안) 번호. 0 = 아직 없음
    lam               REAL,                         -- 재제안 때 같은 목적함수를 쓰기 위해 저장
    -- v3: 승인 정책과 회의 조건. 세션 생성 때 한 번 정해지고 바뀌지 않는다(트리거가 막는다).
    approval_policy_json TEXT NOT NULL DEFAULT '{"mode":"all"}',   -- 기존 세션 = 전원 승인
    approvals_needed     INTEGER,                  -- NULL = 전원
    required_agents_json TEXT NOT NULL DEFAULT '[]',
    constraints_json     TEXT,                     -- 사용자가 확인한 MeetingRequest
    slot_effects_json    TEXT,                     -- {"excluded":[...], "penalty":{"idx":값}}
    parent_session_id    TEXT,                     -- 일정 변경 재협상이 이어받은 원래 세션
    change_request_id    TEXT                      -- 이 세션을 만든 변경 요청
);

CREATE INDEX IF NOT EXISTS idx_sessions_state ON sessions(state);
CREATE INDEX IF NOT EXISTS idx_sessions_group ON sessions(group_id);

-- 참가자 (세션 단위 스냅샷)
CREATE TABLE IF NOT EXISTS participants (
    session_id     TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id       TEXT NOT NULL,          -- HMAC 그룹 가명
    agent_url      TEXT NOT NULL DEFAULT '',
    display_name   TEXT NOT NULL,          -- 승인 화면 표시용
    email          TEXT NOT NULL,          -- attendee 초대용
    is_organizer   INTEGER NOT NULL DEFAULT 0,
    approval_token TEXT NOT NULL,          -- 레거시. 더 이상 승인에 쓰이지 않는다 (approval_tokens 참고)
    PRIMARY KEY (session_id, agent_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_participants_token
    ON participants(approval_token);

-- 사람 승인 라운드(제안) 1건당 1행. 이 테이블이 곧 '시도한 슬롯' 기록이다.
-- UNIQUE(session_id, slot_index): 같은 슬롯을 두 번 제안하려는 시도를 DB 가 거부한다.
CREATE TABLE IF NOT EXISTS proposals (
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,           -- 1 = 1순위 제안, 2 = 2순위 제안 ...
    slot_index  INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN
                    ('open', 'approved', 'rejected', 'conflict', 'expired')),
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    PRIMARY KEY (session_id, version),
    UNIQUE (session_id, slot_index)
);

-- 개별 승인. 공개 채널에 노출하지 않는다.
-- 제안 버전별로 따로 저장하므로 이전 슬롯의 승인은 새 슬롯에 재사용되지 않는다.
-- 거절 사유는 저장하지 않는다 (컬럼 자체가 없다).
CREATE TABLE IF NOT EXISTS approvals (
    session_id       TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id         TEXT NOT NULL,
    proposal_version INTEGER NOT NULL DEFAULT 1,
    verdict          TEXT NOT NULL CHECK (verdict IN ('approve', 'reject')),
    decided_at       TEXT NOT NULL,
    PRIMARY KEY (session_id, proposal_version, agent_id)   -- 중복 클릭 자동 무시
);

-- 승인 링크 토큰의 상태. 토큰 본문은 저장하지 않고 jti 만 저장한다.
-- 서명(HMAC) 없이는 jti 만으로 링크를 만들 수 없다.
CREATE TABLE IF NOT EXISTS approval_tokens (
    jti              TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    proposal_version INTEGER NOT NULL,
    agent_id         TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    used_at          TEXT,                  -- 1회용: 응답을 기록하는 순간 채워진다
    revoked_at       TEXT,                  -- 재발급/제안 종료로 무효화됨
    created_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tokens_target
    ON approval_tokens(session_id, proposal_version, agent_id);

-- 승인 요청 이메일 발송 상태 (제안 버전 × 참가자).
-- 'responded' 는 저장하지 않는다 — 응답 여부는 approvals 에서 읽는다.
CREATE TABLE IF NOT EXISTS notifications (
    session_id       TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    proposal_version INTEGER NOT NULL,
    agent_id         TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN
                        ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,                  -- 비밀번호 등을 제거한 관리자용 메시지
    backend          TEXT,
    sent_at          TEXT,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (session_id, proposal_version, agent_id)
);

-- 양보 잔액. 그룹별로 분리 — 회사 잔액과 친구 잔액은 별개다.
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

-- ---- v3 ---------------------------------------------------------------------

-- 자연어로 입력한 회의 요청의 해석 초안. 사용자가 확인하기 전에는 협상이 시작되지 않는다.
-- request_json 은 서버가 결정론적으로 검증·정규화한 결과다 (LLM 출력을 그대로 믿지 않는다).
CREATE TABLE IF NOT EXISTS meeting_drafts (
    draft_id     TEXT PRIMARY KEY,
    owner        TEXT NOT NULL DEFAULT '',     -- v5: 초안을 만든 관리자. 다른 관리자는 열 수 없다 ('' = 옛 초안)
    group_id     TEXT NOT NULL,
    raw_text     TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('draft', 'started', 'cancelled', 'expired')),
    parser       TEXT NOT NULL,
    session_id   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);

-- 확정된 회의의 변경 요청. 세션마다 진행 중인 요청은 하나뿐이다(부분 UNIQUE 인덱스).
CREATE TABLE IF NOT EXISTS change_requests (
    request_id          TEXT PRIMARY KEY,
    session_id          TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    kind                TEXT NOT NULL CHECK (kind IN ('renegotiate', 'proceed_without', 'cancel')),
    requester_agent_id  TEXT NOT NULL,
    state               TEXT NOT NULL CHECK (state IN
                            ('voting', 'denied', 'expired', 'applying', 'replanning',
                             'applied', 'failed')),
    followup_session_id TEXT,
    fail_reason         TEXT,
    original_start      TEXT NOT NULL,
    original_end        TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_change_one_open
    ON change_requests(session_id) WHERE state IN ('voting', 'applying', 'replanning');

-- 변경 투표. 사유 컬럼은 없다.
CREATE TABLE IF NOT EXISTS change_votes (
    request_id TEXT NOT NULL REFERENCES change_requests(request_id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    verdict    TEXT NOT NULL CHECK (verdict IN ('approve', 'reject')),
    decided_at TEXT NOT NULL,
    PRIMARY KEY (request_id, agent_id)
);

-- 변경 링크 토큰 상태 (요청 링크 / 투표 링크). 토큰 본문은 저장하지 않는다.
CREATE TABLE IF NOT EXISTS change_tokens (
    jti        TEXT PRIMARY KEY,
    purpose    TEXT NOT NULL CHECK (purpose IN ('request', 'vote')),
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    agent_id   TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_tokens_target
    ON change_tokens(session_id, request_id, agent_id, purpose);

-- 변경 관련 이메일 발송 상태. request_id = '' 는 확정 안내(변경 요청 링크 포함) 메일.
CREATE TABLE IF NOT EXISTS change_mail (
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    agent_id   TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('confirm', 'vote', 'result')),
    status     TEXT NOT NULL CHECK (status IN
                   ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    backend    TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, request_id, agent_id, kind)
);

-- ---- v4 ---------------------------------------------------------------------

-- 자연어 회의 요청의 중복 제출 방지. 같은 (scope, key) 는 한 번만 처리하고 같은 결과를 돌려준다.
-- request_hash 는 요청 본문의 해시이며 원문은 저장하지 않는다.
CREATE TABLE IF NOT EXISTS request_keys (
    scope        TEXT NOT NULL,
    key          TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('processing', 'done')),
    draft_id     TEXT,
    session_id   TEXT,
    outcome      TEXT,                       -- started | needs_input | refused | question
    created_at   TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

-- 최소한의 보안 감사 기록. 원문 입력은 저장하지 않는다 (길이와 짧은 해시만).
CREATE TABLE IF NOT EXISTS security_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    kind      TEXT NOT NULL,                 -- blocked_input | login_failed | login_ok | forbidden | rate_limited | csrf
    category  TEXT NOT NULL DEFAULT '',
    actor     TEXT NOT NULL DEFAULT '',
    route     TEXT NOT NULL DEFAULT '',
    text_len  INTEGER,
    text_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_security_events_at ON security_events(at);

-- 회의 자료 분석 결과. PDF 원본은 저장하지 않고 분석 결과만 보관 기간 동안 둔다.
CREATE TABLE IF NOT EXISTS prep_results (
    prep_id     TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    mode        TEXT NOT NULL CHECK (mode IN ('llm', 'rules')),
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    revoked_at  TEXT
);

-- 참석자별 자료 전달 상태. 주최자가 승인해 발송을 시작한 사람만 행이 생긴다.
CREATE TABLE IF NOT EXISTS material_shares (
    prep_id    TEXT NOT NULL REFERENCES prep_results(prep_id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN
                   ('pending', 'sending', 'sent', 'failed', 'revoked')),
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (prep_id, agent_id)
);

-- 자료 열람 링크 토큰 상태. 토큰 본문은 저장하지 않는다.
CREATE TABLE IF NOT EXISTS material_tokens (
    jti        TEXT PRIMARY KEY,
    prep_id    TEXT NOT NULL REFERENCES prep_results(prep_id) ON DELETE CASCADE,
    agent_id   TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_material_tokens_prep ON material_tokens(prep_id, agent_id);
-- ---- v5 ---------------------------------------------------------------------

-- 참가자가 보낸 '일정 변경 요청'. 참가자는 처리 방법을 고르지 않고 요청만 보낸다.
-- 주최자가 처리 방법(재협상 / 요청자 제외 / 취소 제안 / 거절)을 고르기 전에는 아무에게도 투표 메일이 가지 않는다.
-- 사유 컬럼은 없다. 주최자가 재협상·제외·취소를 고르면 기존 change_requests(투표)가 이 요청에서 만들어진다.
CREATE TABLE IF NOT EXISTS change_intakes (
    intake_id          TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    requester_agent_id TEXT NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('open', 'handled', 'declined', 'withdrawn', 'expired')),
    decision           TEXT CHECK (decision IN ('renegotiate', 'proceed_without', 'cancel', 'decline')),
    request_id         TEXT,                       -- 주최자의 선택으로 만들어진 변경 요청(투표)
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    decided_at         TEXT,
    decided_by         TEXT
);
-- 같은 사람이 같은 회의에 열린 요청을 두 번 만들 수 없다 (중복 제출은 한 번만 처리된다)
CREATE UNIQUE INDEX IF NOT EXISTS idx_intake_one_open
    ON change_intakes(session_id, requester_agent_id) WHERE state = 'open';

-- 변경 요청 처리 이력 (감사용). 누가 무엇을 언제 했는지만 남기고 사유는 없다.
CREATE TABLE IF NOT EXISTS change_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    intake_id  TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    at         TEXT NOT NULL,
    event      TEXT NOT NULL,                     -- submitted | decided | vote_mail_queued | applied | failed ...
    actor      TEXT NOT NULL DEFAULT '',          -- participant | organizer | system
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_change_events_session ON change_events(session_id, id);

-- 협상이 시작된 뒤에는 참가자 목록을 바꿀 수 없다 (몰래 넣거나 빼거나 바꾸지 못한다).
-- 세션을 만드는 트랜잭션 안에서는 전이 기록이 아직 없으므로 참가자를 넣을 수 있다.
CREATE TRIGGER IF NOT EXISTS trg_participants_no_late_insert
BEFORE INSERT ON participants
WHEN EXISTS (SELECT 1 FROM transitions WHERE session_id = NEW.session_id)
BEGIN
  SELECT RAISE(ABORT, 'participants are fixed once the session has started');
END;

CREATE TRIGGER IF NOT EXISTS trg_participants_no_delete
BEFORE DELETE ON participants
WHEN EXISTS (SELECT 1 FROM sessions WHERE session_id = OLD.session_id)
BEGIN
  SELECT RAISE(ABORT, 'participants are fixed once the session has started');
END;

CREATE TRIGGER IF NOT EXISTS trg_participants_no_identity_change
BEFORE UPDATE OF agent_id, session_id ON participants
BEGIN
  SELECT RAISE(ABORT, 'participants are fixed once the session has started');
END;
