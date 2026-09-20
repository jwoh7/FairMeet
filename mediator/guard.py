"""자연어 입력 검문: 회의 요청과 그 밖의 요청(질문, 개인정보·권한 초과, 시스템 공격)을 구별한다.

이 모듈은 '어떤 조건을 회의에 적용할지'를 정하지 않는다 (그건 nl_parse/constraints 의 일이다).
여기서는 입력에서 (1) 처리해서는 안 되는 문장을 걸러 내고 (2) 그 이유와 안전한 대안을 만들고
(3) 남은 문장이 회의 요청인지 판단할 뿐이다. 입력은 어떤 경우에도 코드·SQL·셸·경로·URL 로
해석되거나 실행되지 않는다. 거절 문구에는 입력 원문을 되풀이하지 않는다.

한 개의 금칙어 목록이 아니라 서로 다른 신호를 조합한다:
  - 민감한 대상(위치·일정·연락처·기밀 자료·비밀값 ...) + 그것을 내놓으라는 동사 (+ 남의 것이라는 표지)
  - 시스템을 겨냥한 문형 (이전 지시 무시, 파일/DB 읽기, 코드 실행, 권한 부여, 외부 전송 ...)
  - 역할 주장('나는 팀장이다')은 권한의 증거로 세지 않고 무시한다
문장 단위로 검사하고, 문장이 걸리면 절 단위로 다시 나눠 회의 조건 부분은 살린다.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)

# ---- 문형 조각 -------------------------------------------------------------------------------

_DISC = (r"(알려|보여|보내|공유|말해|가르쳐|출력|공개|제공|넘겨|열람|조회|뽑아|정리해|볼\s*수|"
         r"알고\s*싶|추적|읽어|열어|보고\s*싶|show|tell|reveal|give\s+me|send|share|list)")
_DISC_OR_GIVE = r"(" + _DISC[1:-1] + r"|줘|주세요|달라|주라|줄래|줄\s*수)"
_SELF = r"(내|제|저의|나의|우리)\s*"
_OTHERS = (r"(상대방?|그\s*사람|그분|걔|팀원|팀원들|참석자|참가자|동료|직원|멤버|다른\s*(사람|분|팀원|참가자|참석자)|"
           r"각자|각각|모든\s*(사람|팀원|참석자|참가자)|나머지)")

_CAT = {
    "location": ("sensitive",
                 "다른 분의 위치 정보는 알려드릴 수 없어요.",
                 "참가자가 직접 공개한 가능 지역이나 모두가 동의한 장소 후보만 사용할 수 있어요. "
                 "참가자에게 장소 선호를 물어볼까요?"),
    "schedule": ("sensitive",
                 "개인별 일정은 보여드릴 수 없어요. 일정 제목, 장소, 메모, 참석자는 공개되지 않아요.",
                 "FairMeet은 '가능한 시간'만 사용해요. 모두가 가능한 시간을 찾아 드릴까요?"),
    "contact": ("sensitive",
                "연락처는 공유할 수 없어요.",
                "FairMeet 알림으로 메시지를 전달하는 방법을 쓸 수 있어요. 불참 사유도 필요 이상으로 공개되지 않아요."),
    "confidential": ("sensitive",
                     "회의 자료는 회의에 참석하기로 한 분에게만 공유돼요.",
                     "접근이 필요하면 주최자에게 열람 승인을 요청해 주세요."),
    "monopoly": ("sensitive",
                 "한 사람의 의견만 반영할 수는 없어요. 다른 참가자의 응답을 대신하거나 없앨 수도 없어요.",
                 "모든 참가자의 응답과 정해진 승인 기준이 그대로 적용돼요. 필수 참석자와 승인 인원은 "
                 "회의를 요청할 때 정할 수 있어요."),
    "injection": ("attack", "이 요청은 처리할 수 없어요.",
                  "회의 날짜, 참석자, 시간 조건처럼 회의를 잡는 데 필요한 내용을 입력해 주세요."),
    "secrets": ("attack", "비밀값이나 인증 정보는 보여드릴 수 없어요.",
                "회의 날짜, 참석자, 시간 조건처럼 회의를 잡는 데 필요한 내용을 입력해 주세요."),
    "database": ("attack", "내부 데이터에는 접근할 수 없어요.",
                 "회의 날짜, 참석자, 시간 조건처럼 회의를 잡는 데 필요한 내용을 입력해 주세요."),
    "files": ("attack", "서버의 파일이나 경로는 읽을 수 없어요.",
              "회의 날짜, 참석자, 시간 조건처럼 회의를 잡는 데 필요한 내용을 입력해 주세요."),
    "hacking": ("attack", "이 요청은 처리할 수 없어요.",
                "회의 날짜, 참석자, 시간 조건처럼 회의를 잡는 데 필요한 내용을 입력해 주세요."),
    "privilege": ("attack", "권한은 입력한 문장으로 바꿀 수 없어요. 권한은 로그인과 회의별 역할로만 정해져요.",
                  "필요한 권한이 있다면 관리자에게 요청해 주세요."),
    "code": ("attack", "코드나 명령어는 실행하지 않아요.",
             "회의 날짜, 참석자, 시간 조건처럼 회의를 잡는 데 필요한 내용을 입력해 주세요."),
    "url": ("attack", "링크나 외부 주소는 열거나 호출하지 않아요.",
            "회의 날짜, 참석자, 시간 조건처럼 회의를 잡는 데 필요한 내용을 입력해 주세요."),
    "exfil": ("attack", "데이터를 외부로 보낼 수는 없어요.",
              "회의 날짜, 참석자, 시간 조건처럼 회의를 잡는 데 필요한 내용을 입력해 주세요."),
    "others_prompt": ("attack", "다른 사용자의 입력이나 요청 기록은 볼 수 없어요.",
                      "회의 날짜, 참석자, 시간 조건처럼 회의를 잡는 데 필요한 내용을 입력해 주세요."),
}

_ROLE_NOTE = ("'팀장', '관리자' 같은 역할 주장은 권한으로 인정되지 않아요. "
              "권한은 로그인과 회의별 역할로만 정해져요.")


def _c(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


# (분류, 패턴) — 하나라도 맞으면 그 분류로 표시한다.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # 위치: 남의 위치를 내놓으라는 문장. 실시간/현재 위치 계열은 대상 표지 없이도 막는다.
    ("location", _c(r"(위치|주소|동선|이동\s*경로|어디\s*(에\s*)?(있|사|계|살)|사는\s*(곳|동네)|"
                    r"집\s*(이|은)?\s*어디|gps)[^\n]{0,25}" + _DISC)),
    ("location", _c(_DISC + r"[^\n]{0,25}(위치|주소|동선|사는\s*곳)")),
    # 일정 전체 열람 (남의 일정)
    ("schedule", _c(r"(일정|스케줄|캘린더|바쁜\s*시간|시간표|일과)[^\n]{0,25}" + _DISC)),
    ("schedule", _c(_DISC + r"[^\n]{0,15}(일정|스케줄|캘린더|시간표)")),
    # 연락처
    ("contact", _c(r"(연락처|전화\s*번호|휴대폰\s*번호|핸드폰\s*번호|폰\s*번호|이메일\s*주소|메일\s*주소|"
                   r"e-?mail\s*address|카(카오)?톡\s*(아이디|id)|메신저\s*(아이디|id)|개인\s*번호|연락\s*방법)"
                   r"[^\n]{0,20}" + _DISC_OR_GIVE)),
    # 기밀 자료 열람 요구
    ("confidential", _c(r"(기밀|대외비|비공개\s*(문서|자료)|내부\s*(문서|자료)|회의\s*자료|회의\s*내용|회의록|"
                        r"분석\s*결과|첨부\s*(파일|문서)|pdf)[^\n]{0,25}" + _DISC)),
    ("confidential", _c(_DISC + r"[^\n]{0,15}(기밀|대외비|회의\s*자료|회의\s*내용|회의록)")),
    # 권한 독점 / 정책 우회
    ("monopoly", _c(_SELF + r"(의견|생각|뜻|결정|선택)\s*(만|이|은|을|를)?\s*(반영|따라|우선|적용|들어|기준|따르)")),
    ("monopoly", _c(r"(다른|나머지|모든)\s*(사람|팀원|참석자|참가자)(들)?(의|은|는)?\s*"
                    r"(의견|응답|승인|투표|동의)?\S*\s*(무시|삭제|지워|제외|필요\s*없)")),
    ("monopoly", _c(r"(대신|대리(로)?)\s*(승인|동의|투표|응답)")),
    ("monopoly", _c(r"(강제로|무조건)\s*(확정|승인|진행|통과)")),
    ("monopoly", _c(r"(정족수|승인\s*(기준|정책|규칙)|필수\s*참석자|공정\s*(성|하게)?)\S*\s*(을|를|은|는)?\s*"
                    r"(무시|없애|바꿔|변경|해제|우회|끄)")),
    ("monopoly", _c(r"내가\s*(다\s*)?(결정|정할|정하|정해)")),
    # ---- 시스템 공격 ----
    ("injection", _c(r"(이전|앞선|위의|기존|상기|지금까지의|모든)\s*(지시|명령|규칙|지침|프롬프트|설정|안내|제약|제한)"
                     r"\S*\s*(을|를)?\s*(모두\s*|전부\s*)?(무시|잊|취소|어겨|어기|버려|우회|해제|덮어)")),
    ("injection", _c(r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|system)|disregard|"
                     r"forget\s+(everything|all|your)|you\s+are\s+now|pretend\s+to|act\s+as\s+(if|an?|the)")),
    ("injection", _c(r"(시스템|system)\s*(프롬프트|prompt|메시지|지시)|(숨겨진|비공개|내부)\s*(프롬프트|지시|설정|명령|규칙)|"
                     r"(프롬프트|지시문|instructions?)\S*\s*(를|을)?\s*(보여|알려|출력|공개|말해|반복|복사|유출)")),
    ("injection", _c(r"jailbreak|탈옥|dan\s*mode|developer\s*mode|개발자\s*모드|god\s*mode|do\s+anything\s+now")),
    ("injection", _c(r"(규칙|정책|제한|보안|필터)\s*(을|를)?\s*(무시|해제|우회|끄|비활성)|새로운\s*(규칙|지시|명령)")),
    ("secrets", _c(r"(토큰|비밀\s*번호|패스워드|password|passwd|비밀\s*키|시크릿|secret|api\s*[-_]?\s*key|api\s*키|"
                   r"credentials?|자격\s*증명|인증\s*정보|\.env|env\s*파일|환경\s*변수|쿠키|세션\s*(값|id|아이디)|"
                   r"smtp|oauth|(access|refresh)\s*token|private\s*key|개인\s*키)"
                   r"[^\n]{0,25}(보여|알려|출력|공개|줘|주세요|보내|읽어|말해|가르쳐|넘겨|덤프|복사|알고\s*싶|"
                   r"표시|print|show|reveal|tell|dump|give|send)")),
    ("secrets", _c(r"(보여|알려|출력|공개|보내|읽어|말해|가르쳐|덤프|show|print|reveal|tell|dump|give)"
                   r"[^\n]{0,20}(토큰|비밀\s*번호|패스워드|password|비밀\s*키|시크릿|secret|api\s*[-_]?\s*key|"
                   r"credentials?|자격\s*증명|\.env|smtp)")),
    ("database", _c(r"(db|디비|데이터\s*베이스|database|sqlite|fairmeet\.db|모든\s*(데이터|기록|세션|사용자)|"
                    r"전체\s*(데이터|기록))[^\n]{0,25}(전부|전체|모두|덤프|dump|출력|보여|알려|내보내|export|삭제|"
                    r"지워|drop|초기화|읽어)")),
    ("database", _c(r"\b(select|insert|update|delete|drop|alter|create|truncate|union|exec)\b.{0,60}"
                    r"\b(from|into|table|set|where|select|database)\b")),
    ("database", _c(r";\s*(drop|delete|update|insert)\b|'\s*or\s*'?1'?\s*=\s*'?1|\bor\s+1\s*=\s*1|sleep\s*\(|"
                    r"xp_cmdshell|information_schema|sqlite_master|--\s*$|/\*.*\*/")),
    ("files", _c(r"\.\./|\.\.\\|%2e%2e|/etc/(passwd|shadow|hosts)|/proc/|/root/|~/|\b[a-z]:\\|\\\\[a-z0-9.-]+\\|"
                 r"file://")),
    ("files", _c(r"(서버|시스템|로컬|프로젝트|소스)\s*(파일|폴더|디렉터리|디렉토리|코드|로그|설정)")),
    ("files", _c(r"(파일|폴더|디렉터리|디렉토리|소스\s*코드|로그)\S*\s*(을|를)?\s*(읽어|열어|출력|보여|다운로드|가져와)")),
    ("hacking", _c(r"해킹|해커|hack(ing|er)?|exploit|익스플로잇|취약점\s*(공격|이용|찾)|침투|크래킹|무차별\s*대입|"
                   r"brute\s*-?force|ddos|디도스|랜섬|malware|악성\s*코드|reverse\s*shell|리버스\s*쉘|백도어|backdoor|"
                   r"권한\s*상승|sql\s*injection|\bxss\b|\bssrf\b|\brce\b")),
    ("privilege", _c(r"(관리자|admin|어드민|root|루트|슈퍼\s*유저|superuser|sudo|운영자)\s*(권한|계정|모드|로그인|"
                     r"접근|액세스|access|rights?|role)?\s*(을|를|으로|로)?\s*(부여|줘|달라|올려|승격|변경|바꿔|켜|"
                     r"활성|열어|얻|획득|탈취|grant)")),
    ("privilege", _c(r"권한\s*(을|를)?\s*(부여|상승|올려|승격|달라|줘)|(나를|저를|내\s*계정을)\s*(관리자|admin)|"
                     r"(시연|데모)\s*모드\s*(를|을)?\s*(켜|활성|열어|on)")),
    ("code", _c(r"(sql|셸|쉘|shell|bash|zsh|powershell|파워\s*쉘|cmd|명령\s*프롬프트|터미널|python|파이썬|"
                r"자바\s*스크립트|javascript|스크립트|코드|명령어|쿼리|query)\s*(을|를)?\s*(직접\s*)?"
                r"(실행|돌려|run|execute|exec|eval)")),
    ("code", _c(r"subprocess|os\.system|os\.popen|__import__|eval\s*\(|exec\s*\(|import\s+(os|sys|subprocess)|rm\s+-rf|"
                r"del\s+/[fq]|format\s+c:|powershell\s+-|cmd\s*/c|\|\s*(sh|bash)\b|&&|\$\(|`[^`]+`|<\s*/?\s*script|"
                r"javascript:|on(error|load|click)\s*=|<\s*(iframe|img|svg|object|embed)|\{\{|\$\{|%\{|\bcurl\s|"
                r"\bwget\s|\bnc\s+-|nslookup")),
    ("url", _c(r"(https?|ftp|sftp|ssh|gopher|wss?)://|\bdata:[a-z]+/|www\.[a-z0-9-]+\.[a-z]{2,}|"
               r"\b(?:\d{1,3}\.){3}\d{1,3}\b|localhost|\[::1\]|metadata\.google|169\.254\.|\.internal\b|"
               r"\b[a-z0-9-]+\.(com|net|org|io|dev|xyz|ru|cn|kr|co\.kr)\b")),
    ("exfil", _c(r"(외부|다른|제3|별도)\s*(서버|사이트|주소|url|웹훅|webhook|메일|이메일)\S*\s*(로|으로|에)\s*"
                 r".{0,25}(보내|전송|업로드|전달|post|유출|넘겨)")),
    ("exfil", _c(r"webhook|웹훅|pastebin|discord|텔레그램|telegram|(유출|업로드)\s*해")),
    ("others_prompt", _c(r"(다른|타)\s*(사람|사용자|참가자|팀원|유저)(들)?(의|이\s*쓴|이\s*입력한)?\s*"
                         r"(프롬프트|요청|입력|조건|명령|대화|질문|채팅|메시지)\S*\s*(을|를)?\s*.{0,10}"
                         r"(보여|알려|조회|공개|출력|열람|읽어)")),
    ("others_prompt", _c(r"(모든|전체)\s*(요청|프롬프트|입력|대화)\s*(기록|내역|로그)")),
]

# 역할 주장: 권한의 증거로 세지 않고, 문장에서 그 부분만 지운다.
_ROLE_CLAIM = _c(r"((나는|저는|내가|제가|난|전)\s*)?(팀장|관리자|admin|대표|사장|리더|부장|이사|주최자|책임자|"
                 r"매니저|상무|본부장)\s*(으로서|이라서|이니까|이므로|이라|이기\s*때문에|인데|이다|이야|입니다|이에요|이라고)")

_MEETING_NOUN = _c(r"(회의|미팅|약속|모임|면담|세미나|워크숍|워크샵|만나|만남|일정|스케줄|시간)\S*")
_MEETING_ASK = _c(r"(잡아|잡고|잡을|잡자|잡았|만들어|정해|찾아|예약|조율|조정|열고|열자|하고\s*싶|하려|하자|"
                  r"해\s*줘|해줘|해주세요|부탁|필요해|원해|되도록|가능한)")
_TEMPORAL = _c(r"(다음\s*주|이번\s*주|다다음\s*주|내일|모레|오늘|주말|월요일|화요일|수요일|목요일|금요일|토요일|일요일|"
               r"\d+\s*월|\d+\s*일|오전|오후|저녁|아침|점심|\d+\s*시|\d+\s*분|\d+\s*시간|사이|부터|까지|"
               r"[가-힣]{2,4}\s*(와|과|랑|하고|이랑)\s)")
_QUESTION = _c(r"(\?|뭐야|무엇|어떻게|왜|방법|어떤|알려\s*줄|가능해\?|설명)")

_OTHERS_RE = _c(_OTHERS)
_STRONG_SPLIT = re.compile(r"(?<=[.!?。？！])\s+|[\r\n]+")
_CLAUSE_SPLIT = re.compile(r"\s*(?:,|;|，|、|그리고|그런데|근데|그러나|하지만)\s*")


@dataclass
class Refusal:
    category: str
    group: str                    # sensitive | attack
    message: str
    alternative: str

    def to_dict(self) -> dict:
        return {"category": self.category, "group": self.group,
                "message": self.message, "alternative": self.alternative}


@dataclass
class GuardResult:
    intent: str                                   # meeting | question | blocked | empty
    text: str                                     # 회의 조건 해석에 넘길 정리된 문장
    refusals: list[Refusal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def blocked_any(self) -> bool:
        return bool(self.refusals)

    def to_dict(self) -> dict:
        return {"intent": self.intent,
                "refusals": [r.to_dict() for r in self.refusals], "notes": list(self.notes)}


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", str(text or "")).translate(_ZERO_WIDTH)
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def _categories(segment: str, names: tuple[str, ...]) -> set[str]:
    cats: set[str] = set()
    squashed = re.sub(r"\s+", "", segment)
    for cat, pat in _PATTERNS:
        if pat.search(segment) or (len(squashed) != len(segment) and cat not in ("url", "code")
                                   and pat.search(squashed)):
            cats.add(cat)
    # 남의 것인지 확인해야 하는 분류: 표지가 없으면 자기 것/일반 문장으로 보고 풀어 준다.
    others = _OTHERS_RE.search(segment) is not None or any(n and n in segment for n in names)
    if "schedule" in cats and not others:
        cats.discard("schedule")
    if "location" in cats:
        strong = re.search(r"(실시간|현재)\s*위치|gps|사는\s*(곳|동네)|집\s*(주소|이\s*어디)|동선|이동\s*경로",
                           segment, re.IGNORECASE)
        if _c(_SELF + r"(현재\s*)?(위치|주소)").search(segment) and not others:
            cats.discard("location")
        elif not others and not strong:
            cats.discard("location")
    if "confidential" in cats and not re.search(
            r"(기밀|대외비|비공개|내부|회의\s*자료|회의\s*내용|회의록|분석\s*결과|pdf)", segment, re.IGNORECASE):
        cats.discard("confidential")
    # 'url' 은 문서 확장자처럼 보이는 것(예: 회의.com)까지 잡으므로 다른 분류와 겹치면 그쪽을 우선한다.
    if len(cats) > 1 and "url" in cats and not re.search(r"https?://|www\.|localhost|\d+\.\d+\.\d+\.\d+", segment,
                                                        re.IGNORECASE):
        cats.discard("url")
    return cats


def _looks_like_meeting(text: str) -> bool:
    if not text.strip():
        return False
    noun = _MEETING_NOUN.search(text) is not None
    ask = _MEETING_ASK.search(text) is not None
    when = _TEMPORAL.search(text) is not None
    if _QUESTION.search(text) and not ask:
        return False
    return (noun and (ask or when)) or (ask and when)


def inspect(text: str, names: tuple[str, ...] | list[str] = ()) -> GuardResult:
    """입력을 검문한다. 항상 결과를 돌려주고 예외를 던지지 않는다."""
    names = tuple(n for n in names if n)
    t = normalize(text)
    if not t:
        return GuardResult("empty", "")

    refusals: dict[str, Refusal] = {}
    notes: list[str] = []

    def refuse(cat: str) -> None:
        if cat not in refusals:
            group, msg, alt = _CAT[cat]
            refusals[cat] = Refusal(cat, group, msg, alt)

    kept: list[str] = []
    for sentence in _STRONG_SPLIT.split(t):
        if not sentence.strip():
            continue
        cats = _categories(sentence, names)
        # 역할 주장은 지우고 알린다 (그 문장의 나머지 회의 조건은 그대로 둔다).
        if _ROLE_CLAIM.search(sentence):
            stripped = _ROLE_CLAIM.sub(" ", sentence).strip(" ,;")
            if _ROLE_NOTE not in notes:
                notes.append(_ROLE_NOTE)
            if not cats:
                sentence = stripped
                if not sentence:
                    continue
        if not cats:
            kept.append(sentence)
            continue
        clauses = [c for c in _CLAUSE_SPLIT.split(sentence) if c.strip()]
        survivors, flagged_any = [], False
        for clause in clauses:
            c_cats = _categories(clause, names)
            if c_cats:
                flagged_any = True
                for cat in c_cats:
                    refuse(cat)
            else:
                survivors.append(clause)
        if not flagged_any:                 # 문장 전체로 볼 때만 걸렸다: 문장을 통째로 버린다
            for cat in cats:
                refuse(cat)
        elif survivors:
            # 걸린 절만 빼고 남긴다. 남은 절이 조건을 이루는지는 이후 해석 단계가 판단한다.
            for cat in cats:
                refuse(cat)
            kept.append(", ".join(survivors))

    clean = " ".join(kept).strip()
    ordered = sorted(refusals.values(), key=lambda r: (r.group != "attack", r.category))
    if not clean:
        return GuardResult("blocked" if ordered else "empty", "", ordered, notes)
    if _looks_like_meeting(clean):
        return GuardResult("meeting", clean, ordered, notes)
    if ordered:                                  # 걸러 낸 뒤 남은 것이 회의 요청이 아니면 요청 전체를 거절한다
        return GuardResult("blocked", "", ordered, notes)
    return GuardResult("question", clean, ordered, notes)
