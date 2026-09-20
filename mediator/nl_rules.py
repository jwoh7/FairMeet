"""규칙 기반 자연어 회의 조건 추출기 (한국어). 외부 호출이 없고 항상 같은 결과를 낸다.

이 파일이 하는 일은 '문장 → 조건 후보(raw dict)'까지다. raw 는 LLM 경로의 출력과 같은
형식이며, 어느 쪽이든 nl_parse.build_request 와 constraints.evaluate_request 의
결정론적 검증을 다시 거친다.

원칙
  - 자연어를 코드·SQL·셸로 실행하지 않는다. 문자열 매칭과 산술만 한다.
  - 시스템 변경/명령/비밀값 요청으로 보이는 문장은 통째로 무시하고 사용자에게 알린다.
  - 해석하지 못한 문장은 조용히 버리지 않는다: type='other'(자동 처리 불가)로 남겨
    사용자가 직접 확인하거나 제거하게 한다.
  - 모호한 표현(너무 늦은 저녁의 기준, 3시가 오전/오후인지 등)은 임의로 정하지 않고
    ask 로 확인 질문 대상으로 표시한다.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata

WEEKDAYS = "월화수목금토일"

# 이 종류의 문장은 조건이 아니라 '시스템에 대한 지시'일 가능성이 높다 → 무시하고 알린다.
_INJECTION = [re.compile(p, re.I) for p in (
    r"ignore\s+(?:all\s+|the\s+|any\s+)?(?:previous|above|prior|earlier)",
    r"disregard\s+(?:all\s+|the\s+)?(?:previous|above|prior|instructions?|rules?)",
    r"(?:forget|override|bypass)\s+(?:all\s+|your\s+|the\s+)?(?:instructions?|rules?|guidelines?)",
    r"system\s*prompt", r"developer\s*message", r"you\s+are\s+now\b", r"act\s+as\b",
    r"이전\s*(?:의\s*)?(?:지시|명령|프롬프트|지침|규칙)",
    r"(?:지시|명령|규칙|지침|설정|프롬프트)\s*(?:을|를)?\s*(?:무시|잊어|변경|바꿔|바꾸|덮어|초기화)",
    r"시스템\s*(?:프롬프트|설정|명령|메시지|지시)",
    r"프롬프트\s*(?:를|을)?\s*(?:출력|공개|보여|알려)",
    r"(?:api|API)\s*(?:키|key)", r"비밀\s*(?:번호|키|값)",
    r"(?:토큰|비밀번호|패스워드|password|secret|credential)s?\s*(?:값|을|를|이|가)?\s*"
    r"(?:알려|출력|보여|공개|말해|복사|전송|보내)",
    r"\.env\b", r"credentials\.json", r"smtp[_\s-]*(?:password|pass|비밀번호)",
    r"\b(?:drop|delete|truncate|alter|insert|update|attach)\s+(?:table|from|into|database)\b",
    r"\bselect\b[^.]{0,60}\bfrom\b", r";\s*--", r"\bunion\s+select\b",
    r"\b(?:rm\s+-rf|sudo|chmod|chown|curl|wget|powershell|cmd\.exe|bash\s+-c|nc\s+-e)\b",
    r"\b(?:eval|exec|system|popen|subprocess|os\.system)\s*\(",
    r"```", r"<\s*/?\s*(?:script|system|assistant|user|iframe)\b", r"\{\{.*?\}\}", r"\$\(.*?\)",
    r"(?:모든|전체)\s*(?:세션|데이터|일정|기록|DB|데이터베이스)\s*(?:를|을)?\s*(?:삭제|지워|초기화|리셋)",
    r"(?:관리자|admin|root)\s*(?:권한|모드|계정)",
    r"(?:승인|동의|거절)\s*(?:을|를)?\s*(?:대신|자동으로|강제로)\s*(?:해|처리|눌러)",
    r"(?:이메일|메일)\s*(?:을|를)?\s*(?:대량|전체)\s*(?:발송|전송)",
    r"(?:서버|프로세스)\s*(?:를|을)?\s*(?:종료|재시작|중지|삭제)",
)]

MAX_CLAUSES = 60


class InvalidText(ValueError):
    """입력 문장 자체가 처리할 수 없는 경우 (비어 있음, 너무 김, 문자열이 아님)."""


def sanitize_text(text, max_chars: int) -> str:
    """제어 문자를 지우고 공백을 정리한다. 내용을 바꾸거나 잘라 쓰지 않는다."""
    if not isinstance(text, str):
        raise InvalidText("문장을 입력해 주세요")
    t = unicodedata.normalize("NFKC", text)
    t = "".join(ch if ch == "\n" or unicodedata.category(ch)[0] != "C" else " " for ch in t)
    t = re.sub(r"[ \t ]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if not t:
        raise InvalidText("문장을 입력해 주세요")
    if len(t) > max_chars:
        raise InvalidText(f"입력이 너무 깁니다 (최대 {max_chars}자, 현재 {len(t)}자)")
    return t


def detect_injection(sentence: str) -> bool:
    return any(p.search(sentence) for p in _INJECTION)


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。])\s+|\n+", text)
    return [p.strip() for p in parts if p and p.strip()][:MAX_CLAUSES]


_CLAUSE_SPLIT = re.compile(
    r"(?<=빼고)\s+|(?<=제외하고)\s+|(?<=피하고)\s+|(?<=피해서)\s+|(?<=하되)\s+|(?<=잡되)\s+|"
    r"(?<=지만)\s+|(?<=인데)\s+|(?<=는데)\s+|(?<=이며)\s+|(?<=이고)\s+|"
    r"(?<=해야 하고)\s+|(?<=해야하고)\s+|(?<=하면서)\s+|(?<=이면서)\s+|,\s*(?=[가-힣A-Za-z])")


def split_clauses(sentence: str) -> list[str]:
    return [c.strip(" ,") for c in _CLAUSE_SPLIT.split(sentence) if c and c.strip(" ,")]


# ---- 표현 사전 ---------------------------------------------------------------------------

_EXCL = re.compile(r"빼고|빼줘|빼주세요|빼 줘|빼\s*고|제외|피하|피해|피할|불가|안\s*돼|안\s*됩니다|"
                   r"안\s*되|안\s*될|안\s*됨|어려워|어렵|못\s*해|곤란|싫어|힘들|힘든|불편")
_SOFT_AVOID = re.compile(r"피하|피해|피할|가능하면|되도록|웬만하면|가급적|가능한\s*한")
_PREF = re.compile(r"가능한\s*한|가능하면|되도록|웬만하면|가급적|우선|선호|좋겠|좋아|위주|"
                   r"중심으로|이면\s*좋|였으면|했으면")
_REQ = re.compile(r"반드시|꼭|무조건|필수|만\s*가능|에만|만\s*돼|만\s*됩니다|이어야|여야")
_SCHED_VERB = re.compile(r"잡아|잡자|잡고|해줘|해\s*주세요|하자|만나|진행|열어|정해|배치|부탁|찾아")

_TOD = {
    "새벽": ("00:00", "06:00"), "아침": ("06:00", "09:00"), "오전": ("06:00", "12:00"),
    "점심시간": ("12:00", "13:00"), "점심": ("12:00", "13:00"), "오후": ("12:00", "18:00"),
    "저녁": ("18:00", "22:00"), "밤": ("21:00", "24:00"),
}
_KO_NUM = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8,
           "아홉": 9, "열": 10}

_CLOCK_RANGE = re.compile(
    r"(?:(?P<ap1>오전|오후)\s*)?(?P<h1>\d{1,2})\s*시(?!간)(?:\s*(?P<m1>\d{1,2})\s*분)?\s*"
    r"(?:부터|~|-|에서)\s*(?:(?P<ap2>오전|오후)\s*)?(?P<h2>\d{1,2})\s*시(?!간)"
    r"(?:\s*(?P<m2>\d{1,2})\s*분)?(?:\s*(?:까지|사이))?")
_CLOCK_ONE = re.compile(
    r"(?:(?P<ap>오전|오후)\s*)?(?P<h>\d{1,2})\s*시(?!간)(?:\s*(?P<m>\d{1,2})\s*분|\s*(?P<half>반))?"
    r"(?:\s*(?P<dir>이후|부터|이전|까지|전|후))?")
_ATOM = re.compile(
    r"(?P<date>\d{1,2}\s*월\s*\d{1,2}\s*일)"
    r"|(?P<crange>" + _CLOCK_RANGE.pattern.replace("?P<", "?P<r_") + r")"
    r"|(?P<clock>" + _CLOCK_ONE.pattern.replace("?P<", "?P<c_") + r")"
    r"|(?P<late>너무\s*늦은\s*(?:저녁|시간|밤)|늦은\s*(?:저녁|시간대|시간|밤)|밤늦게)"
    r"|(?P<early>너무\s*이른\s*(?:아침|시간)|이른\s*(?:아침|시간대|시간))"
    r"|(?P<tod>점심\s*시간|점심시간|점심|오전|오후|저녁|아침|새벽|밤)"
    r"|(?P<wdgrp>평일|주말)"
    r"|(?P<wdrange>[월화수목금토일]요일\s*(?:부터|~|-)\s*[월화수목금토일]요일(?:\s*(?:까지|사이))?)"
    r"|(?P<wd>[월화수목금토일]요일)")


def _weekday_range(w1: str, w2: str) -> list[int]:
    """'월요일부터 수요일' → [0,1,2] 처럼 두 요일 사이(포함)를 이어진 요일 목록으로 편다.

    끝 요일이 시작보다 앞이면('금요일부터 월요일') 주를 넘어가는 것으로 보고 이어 붙인다."""
    i1, i2 = WEEKDAYS.index(w1), WEEKDAYS.index(w2)
    return list(range(i1, i2 + 1)) if i1 <= i2 else list(range(i1, 7)) + list(range(0, i2 + 1))

_QTY = re.compile(
    r"(?:(?P<hn>한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)|(?P<hd>\d+(?:\.\d+)?))\s*시간"
    r"(?:\s*(?P<half>반))?(?:\s*(?P<m1>\d{1,3})\s*분)?"
    r"|(?P<half2>반)\s*시간|(?P<m2>\d{1,3})\s*분")
_BUF_WORDS = re.compile(r"여유|이동|버퍼|쉬는|앞뒤|전후|끝난|끝나고|마친|이후에|뒤에|전에|쉬고|"
                        r"휴식|준비|비워|확보")


# ---- 시간 명세 ------------------------------------------------------------------------------

def _hhmm(h: int, m: int = 0) -> str:
    return f"{h:02d}:{m:02d}"


def _to_24h(h: int, ap: str | None) -> tuple[int, bool]:
    """(24시간제 시, 오전/오후가 모호한가). 표기가 없고 1~7시면 오후로 가정하고 모호함을 표시."""
    if ap == "오후":
        return (h + 12 if h < 12 else h), False
    if ap == "오전":
        return (0 if h == 12 else h), False
    if 1 <= h <= 7:
        return h + 12, True
    return h, False


def parse_time_groups(fragment: str, now: dt.datetime) -> list[dict]:
    """조각 안의 요일·날짜·시간대·시각을 묶음(spec)으로 정리한다.

    반환: [{"spec": {weekdays,dates,start_time,end_time,label}, "ask": [...],
            "ampm_fields": [...]}, ...]
    같은 묶음 규칙: 요일/날짜는 '이미 시간 표현이 붙은 묶음' 뒤에 오면 새 묶음을 연다.
      '월요일 오전과 금요일' → [월 오전], [금]
      '월요일과 수요일 오전' → [월·수 오전]
    """
    groups: list[dict] = []
    cur: dict | None = None

    def new():
        g = {"weekdays": [], "dates": [], "start": None, "end": None, "label": None,
             "has_time": False, "clock_set": False, "ask": [], "ampm_fields": []}
        groups.append(g)
        return g

    def ap_hint(label):
        """'저녁 7시' 처럼 시간대 낱말이 앞에 있으면 오전/오후를 묻지 않는다."""
        if label in ("오후", "저녁", "밤", "점심", "점심시간"):
            return "오후"
        if label in ("오전", "아침", "새벽"):
            return "오전"
        return None

    for m in _ATOM.finditer(fragment):
        kind = m.lastgroup
        # finditer 의 lastgroup 은 가장 안쪽 이름이라 최상위 이름을 다시 찾는다
        for top in ("date", "crange", "clock", "late", "early", "tod", "wdgrp", "wdrange", "wd"):
            if m.group(top) is not None:
                kind = top
                break

        if kind in ("date", "wd", "wdgrp", "wdrange"):
            if cur is None or cur["has_time"]:
                cur = new()
            if kind == "date":
                mo, dy = map(int, re.findall(r"\d+", m.group("date")))
                try:
                    d = dt.date(now.year, mo, dy)
                except ValueError:
                    continue
                if (now.date() - d).days > 180:
                    d = dt.date(now.year + 1, mo, dy)
                iso = d.isoformat()
                if iso not in cur["dates"]:
                    cur["dates"].append(iso)
            elif kind == "wdgrp":
                days = [0, 1, 2, 3, 4] if m.group("wdgrp") == "평일" else [5, 6]
                cur["weekdays"] = sorted(set(cur["weekdays"]) | set(days))
            elif kind == "wdrange":
                # 요일 글자 자체('일')가 '요일' 안에도 있어 그냥 문자 클래스로 훑으면 안 된다:
                # 반드시 '요일' 앞에 오는 글자만 요일로 센다.
                w1, w2 = re.findall(r"([월화수목금토일])요일", m.group("wdrange"))[:2]
                cur["weekdays"] = sorted(set(cur["weekdays"]) | set(_weekday_range(w1, w2)))
            else:
                wd = WEEKDAYS.index(m.group("wd")[0])
                if wd not in cur["weekdays"]:
                    cur["weekdays"] = sorted(cur["weekdays"] + [wd])
            continue

        # 시간 표현. 시각(clock)은 바로 앞의 시간대 낱말('저녁 7시')과 한 묶음으로 본다.
        is_clock = kind in ("crange", "clock")
        if cur is None:
            cur = new()
        elif cur["has_time"] and not (is_clock and not cur["clock_set"]):
            cur = new()
        merged_tod = cur["has_time"]
        cur["has_time"] = True

        if kind == "crange":
            g = m.groupdict()
            hint = ap_hint(cur["label"]) if merged_tod else None
            h1, a1 = _to_24h(int(g["r_h1"]), g["r_ap1"] or hint)
            ap2 = g["r_ap2"] or (g["r_ap1"] if g["r_ap1"] == "오후" else None) or hint
            h2, a2 = _to_24h(int(g["r_h2"]), ap2)
            if h2 <= h1 and h2 < 12:
                h2 += 12                                  # '5시부터 9시' → 17:00~21:00
            cur["start"] = _hhmm(h1, int(g["r_m1"] or 0))
            cur["end"] = _hhmm(h2, int(g["r_m2"] or 0))
            cur["clock_set"] = True
            if a1 or a2:
                cur["ask"].append("ampm")
                cur["ampm_fields"] += ["start_time", "end_time"]
        elif kind == "clock":
            g = m.groupdict()
            hint = ap_hint(cur["label"]) if merged_tod else None
            h, amb = _to_24h(int(g["c_h"]), g["c_ap"] or hint)
            mi = 30 if g["c_half"] else int(g["c_m"] or 0)
            t = _hhmm(h, mi)
            direction = g["c_dir"]
            field = "end_time" if direction in ("이전", "까지", "전") else "start_time"
            cur[{"start_time": "start", "end_time": "end"}[field]] = t
            cur["clock_set"] = True
            if merged_tod and direction:                  # '저녁 7시 이후' → 7시부터 열려 있다
                cur["end" if field == "start_time" else "start"] = None
            if amb:
                cur["ask"].append("ampm")
                cur["ampm_fields"].append(field)
        elif kind == "late":
            cur["start"], cur["end"], cur["label"] = "19:00", "24:00", "늦은 시간"
            cur["ask"].append("late_threshold")
        elif kind == "early":
            cur["start"], cur["end"], cur["label"] = None, "09:00", "이른 시간"
            cur["ask"].append("early_threshold")
        else:  # tod
            word = re.sub(r"\s+", "", m.group("tod"))
            t0, t1 = _TOD[word]
            if cur["start"] is None and cur["end"] is None:
                cur["start"], cur["end"] = t0, t1
            cur["label"] = word if word != "점심시간" else "점심시간"

    out = []
    for g in groups:
        spec = {}
        if g["dates"]:
            spec["dates"] = g["dates"]
        if g["weekdays"]:
            spec["weekdays"] = g["weekdays"]
        if g["start"]:
            spec["start_time"] = g["start"]
        if g["end"]:
            spec["end_time"] = g["end"]
        if g["label"]:
            spec["label"] = g["label"]
        if spec:
            out.append({"spec": spec, "ask": list(dict.fromkeys(g["ask"])),
                        "ampm_fields": list(dict.fromkeys(g["ampm_fields"]))})
    return out


# ---- 날짜 범위 ------------------------------------------------------------------------------

def _monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _next_week_start(today: dt.date) -> dt.date:
    return today + dt.timedelta(days=(7 - today.weekday()) % 7 or 7)


def date_range_from_clause(clause: str, now: dt.datetime, allow_specific_day: bool):
    """한 조각에서 탐색 기간 표현을 찾는다. 없으면 None.

    반환: {"anchor": ..., "start": iso|None, "end": iso|None, "weeks": int|None}
    """
    today = now.date()
    c = clause

    m = re.search(r"(다다음|다음|이번)\s*주\s*(?:의\s*)?([월화수목금토일])요일", c)
    one_day = len(re.findall(r"[월화수목금토일]요일", c)) == 1     # '화요일이나 수요일' 은 기간이 아니다
    if m and allow_specific_day and one_day:
        base = {"이번": _monday(today), "다음": _next_week_start(today),
                "다다음": _next_week_start(today) + dt.timedelta(days=7)}[m.group(1)]
        d = base + dt.timedelta(days=WEEKDAYS.index(m.group(2)))
        return {"anchor": "explicit", "start": d.isoformat(), "end": d.isoformat(),
                "weeks": None}

    dates = []
    for mm in re.finditer(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", c):
        try:
            d = dt.date(today.year, int(mm.group(1)), int(mm.group(2)))
        except ValueError:
            continue
        if (today - d).days > 180:
            d = dt.date(today.year + 1, d.month, d.day)
        dates.append(d)
    m2 = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일\s*(?:부터|~|-)\s*(\d{1,2})\s*일", c)
    if m2:
        try:
            d2 = dt.date(dates[0].year, int(m2.group(1)), int(m2.group(3)))
            dates = [dates[0], d2]
        except (ValueError, IndexError):
            pass
    if dates and not (EXCL_OR_PREF(c) and len(dates) == 1):
        lo, hi = min(dates), max(dates)
        return {"anchor": "explicit", "start": lo.isoformat(), "end": hi.isoformat(),
                "weeks": None}

    if re.search(r"다다음\s*주", c):
        return {"anchor": "week_after_next", "start": None, "end": None, "weeks": None}
    if re.search(r"다음\s*주", c):
        return {"anchor": "next_week", "start": None, "end": None, "weeks": None}
    if re.search(r"이번\s*주", c):
        return {"anchor": "this_week", "start": None, "end": None, "weeks": None}
    mw = re.search(r"(\d{1,2})\s*주\s*(?:안|내|이내|동안|안에)", c)
    if mw and 1 <= int(mw.group(1)) <= 4:
        return {"anchor": "within_weeks", "start": None, "end": None,
                "weeks": int(mw.group(1))}
    if re.search(r"모레|내일모레", c):
        return {"anchor": "day_after_tomorrow", "start": None, "end": None, "weeks": None}
    if re.search(r"내일", c):
        return {"anchor": "tomorrow", "start": None, "end": None, "weeks": None}
    if re.search(r"오늘", c):
        return {"anchor": "today", "start": None, "end": None, "weeks": None}
    return None


def EXCL_OR_PREF(c: str) -> bool:
    return bool(_EXCL.search(c) or _PREF.search(c))


# ---- 추출 -----------------------------------------------------------------------------------

_JOSA = (r"(?:이랑|이고|이며|이라|이야|이다|입니다|이에요|한테|에게|"
         r"은|는|이|가|도|만|을|를|의|님|씨|과|와|랑)?")
_STOP_NAMES = {"나머지", "모두", "전원", "우리", "다들", "다른", "사람", "참석자", "참가자", "저",
               "제", "내", "본인", "각자", "서로", "팀원", "다같이", "회의", "미팅", "시간", "그룹"}
_PART_VERB = r"(?:참석|참여|출석|와야|나와야|있어야|필참|와줘|들어와)"


def _find_names(text: str, names: list[str]) -> list[str]:
    hits = []
    for n in names:
        for m in re.finditer(r"(?<![가-힣])" + re.escape(n) + _JOSA + r"(?![가-힣])", text):
            hits.append((m.start(), n))
    return [n for _, n in sorted(hits)]


def _unknown_name_tokens(text: str, names: list[str]) -> list[str]:
    """필수 표현 바로 앞에 붙은, 참가자 목록에 없는 이름 후보."""
    out = []
    for m in re.finditer(r"(?<![가-힣])([가-힣]{2,4})\s*(?:은|는|이|가|만|도)?\s*"
                         r"(?:반드시|꼭|무조건|필수)", text):
        cand = m.group(1)
        stripped = cand[:-1] if len(cand) >= 3 and cand[-1] in "은는이가만도" else cand
        if any(v in names or v in _STOP_NAMES for v in (cand, stripped)):
            continue
        if any(cand.startswith(n) for n in names):        # '재원만' 처럼 조사가 붙은 알려진 이름
            continue
        if stripped not in out:
            out.append(stripped)
    return out


def _required_by_name(s: str, names: list[str]) -> list[str]:
    """'…가능하고 재원, 도연은 필수야' 처럼 '참석' 없이 이름과 '필수'만 말한 경우."""
    m = re.search(r"필수(?!\s*(?:시간|조건|일정|적으로|참석자))", s)
    if not m:
        return []
    prefix = s[:m.start()]
    tail = re.split(r"하고|이고|이며|지만|인데|는데|해서|해야\s*하고", prefix)[-1]
    return _find_names(tail, names)


def _qty_minutes(m: re.Match) -> int | None:
    g = m.groupdict()
    if g["m2"] is not None:
        return int(g["m2"])
    if g["half2"]:
        return 30
    hours = _KO_NUM.get(g["hn"]) if g["hn"] else float(g["hd"])
    if hours is None:
        return None
    mins = round(hours * 60) + (30 if g["half"] else 0) + int(g["m1"] or 0)
    return mins


class _Extract:
    def __init__(self, names: list[str], now: dt.datetime):
        self.names, self.now = names, now
        self.constraints: list[dict] = []
        self.durations: list[int] = []
        self.title = None
        self.purpose = None
        self.priority = "normal"
        self.date_range = None
        self.parse_notes: list[str] = []

    def add(self, typ, value, strength, priority, source, ask=None, data=None):
        self.constraints.append({"type": typ, "value": value, "strength": strength,
                                 "priority": priority, "source_text": source,
                                 "ask": list(ask or []), "ask_data": data or {}})


def rules_extract(sentences: list[str], names: list[str], now: dt.datetime) -> dict:
    """문장 목록 → raw dict. names 는 참가자 이름(예: 재원, 도연, 승현)."""
    ex = _Extract(names, now)
    for s in sentences:
        _sentence(ex, s)

    # 같은 승인 기준을 여러 문장이 말했으면 하나로 합친다 (서로 다른 기준일 때만 충돌이다)
    seen_quorum, deduped = set(), []
    for c in ex.constraints:
        if c["type"] == "quorum":
            key = repr(sorted(c["value"].items()))
            if key in seen_quorum:
                continue
            seen_quorum.add(key)
        deduped.append(c)
    ex.constraints = deduped

    raw = {"title": ex.title, "purpose": ex.purpose, "priority": ex.priority,
           "duration_minutes": None, "duration_candidates": [],
           "date_range": ex.date_range or {"anchor": "none", "start": None, "end": None,
                                          "weeks": None},
           "constraints": ex.constraints, "notes": list(dict.fromkeys(ex.parse_notes))}
    uniq = list(dict.fromkeys(ex.durations))
    if len(uniq) == 1:
        raw["duration_minutes"] = uniq[0]
    elif len(uniq) > 1:
        raw["duration_candidates"] = uniq
    return raw


_FILLER = re.compile(r"^(?:안녕|부탁|감사|고마|수고|잘\s*부탁|please|thanks|hi\b|hello)", re.I)


def _sentence(ex: _Extract, s: str) -> None:
    names = ex.names
    consumed = False
    clock_free = _CLOCK_RANGE.sub(" ", s)
    clock_free = _CLOCK_ONE.sub(" ", clock_free)

    # ---- 긴급도 / 제목 / 목적 ----
    if re.search(r"긴급|급해|급합니다|급한", s):
        ex.priority, consumed = "high", True
    mt = re.search(r"[\"'“”‘’「『]([^\"'“”‘’」』]{2,40})[\"'“”‘’」』]", s)
    if mt and ex.title is None:
        ex.title, consumed = mt.group(1).strip(), True
    else:
        mt2 = re.search(r"(?:^|[\s,])([가-힣A-Za-z0-9]{2,12}(?:\s[가-힣A-Za-z0-9]{2,12})?)\s*"
                        r"(회의|미팅|면담|스터디|리뷰|워크숍|발표)(?:를|을|는|은|가|이)?\s*"
                        r"(?:잡|하|열|진행|만들|정)", s)
        if mt2 and ex.title is None and mt2.group(1) not in _STOP_NAMES \
                and not re.fullmatch(r"(?:다음|이번|다다음|내일|모레|오늘|온라인|대면|첫|새로운|긴급)", mt2.group(1)) \
                and not re.search(r"\d+\s*(?:분|시간)", mt2.group(1)) and not _find_names(mt2.group(1), names):
            ex.title, consumed = f"{mt2.group(1)} {mt2.group(2)}", True
    mp = re.search(r"(?:안건|주제|목적)\s*(?:은|는|:)\s*([^.\n]{2,80})", s)
    if mp and ex.purpose is None:
        ex.purpose, consumed = mp.group(1).strip(), True

    # ---- 시간 분량: 소요 시간 / 여유 시간 ----
    buf_qty, dur_qty = [], []
    for m in _QTY.finditer(clock_free):
        minutes = _qty_minutes(m)
        if minutes is None or minutes <= 0:
            continue
        ctx = clock_free[max(0, m.start() - 12): m.end() + 14]
        (buf_qty if _BUF_WORDS.search(ctx) else dur_qty).append(minutes)
    for minutes in dur_qty:
        ex.durations.append(minutes)
        consumed = True
    if buf_qty:
        _buffer(ex, s, buf_qty[0])
        consumed = True
    if _b2b(ex, s):
        consumed = True

    # ---- 승인 정책 / 필수 참석자 ----
    if _participants(ex, s):
        consumed = True

    # ---- 회의 방식 / 장소 / 반복 / 접근성 / 외부 조건 / 빠른 시간 ----
    for fn in (_mode, _location, _recurrence, _accessibility, _external, _earliest):
        if fn(ex, s):
            consumed = True

    # ---- 절 단위: 탐색 기간과 시간대 ----
    for clause in split_clauses(s):
        if _clause_time(ex, clause, s):
            consumed = True

    if not consumed and len(re.sub(r"\W", "", s)) > 2 and not _FILLER.search(s):
        ex.add("other", {"text": s[:200]}, "soft", 3, s)


def _clause_time(ex: _Extract, clause: str, sentence: str) -> bool:
    now = ex.now
    marked = EXCL_OR_PREF(clause) or bool(_REQ.search(clause))
    consumed = False

    dr = date_range_from_clause(clause, now, allow_specific_day=not marked)
    if dr is not None:
        if ex.date_range is None:
            ex.date_range = dr
        elif ex.date_range != dr:
            ex.parse_notes.append(f"여러 기간 표현이 있어 첫 번째('{ex.date_range['anchor']}')를 "
                                  "사용했습니다. 기간을 확인해 주세요.")
        consumed = True

    # 기간 표현에 쓰인 요일('다음 주 화요일')은 시간 조건으로 다시 세지 않는다
    body = clause
    if dr is not None and dr["anchor"] == "explicit":
        body = re.sub(r"(다다음|다음|이번)\s*주\s*(?:의\s*)?[월화수목금토일]요일", " ", body)
        body = re.sub(r"\d{1,2}\s*월\s*\d{1,2}\s*일(?:\s*(?:부터|~|-)\s*\d{1,2}\s*일)?", " ", body)
    groups = parse_time_groups(body, now)
    if not groups:
        return consumed                               # 기간만 말한 절이거나 시간 표현이 없는 절

    neg = bool(_EXCL.search(clause))
    soft_avoid = bool(_SOFT_AVOID.search(clause))
    req = bool(_REQ.search(clause)) and not re.search(_PART_VERB, clause)
    pref = bool(_PREF.search(clause))
    sched = bool(_SCHED_VERB.search(clause))
    if not (neg or req or pref or sched):
        return consumed                              # 시간 표현이 있어도 조건 문맥이 아님

    for g in groups:
        if neg:
            typ, strength, prio = "excluded_time", ("soft" if soft_avoid else "hard"), \
                (4 if soft_avoid else 3)
        elif req:
            typ, strength, prio = "time_window", "hard", 3
        elif pref:
            typ, strength, prio = "preferred_time", "soft", 3
        else:
            typ, strength, prio = "preferred_time", "soft", 4
        ask_data = {"ampm_fields": g["ampm_fields"]}
        ex.add(typ, g["spec"], strength, prio, clause, g["ask"], ask_data)
    return True


def _buffer(ex: _Extract, s: str, minutes: int) -> None:
    both = bool(re.search(r"전후|앞뒤|양쪽|앞\s*뒤", s))
    after = bool(re.search(r"끝난|끝나고|마친|이후|뒤에|후에|다음", s))
    before = bool(re.search(r"시작\s*전|전에|이전|앞에", s))
    if both or (after and before):
        b = a = minutes
    elif after:
        b, a = 0, minutes
    elif before:
        b, a = minutes, 0
    else:
        b = a = minutes if "여유" in s else 0
        if not (b or a):
            b, a = 0, minutes                       # '이동 시간' 등: 끝난 뒤로 해석
    hard = bool(re.search(r"필요|필수|반드시|꼭|해야|있어야|확보", s)) \
        and not re.search(r"가능하면|되도록|웬만하면", s)
    who = _find_names(s, ex.names)
    ask = ["buffer_target"] if re.search(r"수업|강의|수강|학원|근무|출근|퇴근", s) else []
    ex.add("buffer_time", {"before_min": b, "after_min": a, "participants": who},
           "hard" if hard else "soft", 3 if hard else 4, s, ask)


def _b2b(ex: _Extract, s: str) -> bool:
    if not re.search(r"연속\s*(?:으로)?\s*(?:회의|미팅|일정)|연달아|back\s*-?\s*to\s*-?\s*back|"
                     r"쉬는\s*시간\s*없이|붙어\s*있|이어지", s, re.I):
        return False
    if not re.search(r"않게|없게|피하|피해|안\s*되게|되지\s*않|안\s*돼|막아|줄여|말고", s):
        return False
    who = _find_names(s, ex.names)
    ex.add("buffer_time", {"before_min": 15, "after_min": 15, "participants": who},
           "soft", 4, s, ["buffer_amount"])
    return True


def _participants(ex: _Extract, s: str) -> bool:
    consumed = False
    names = ex.names
    got = False

    # 승인 기준
    m_others = re.search(r"나머지\s*(?:는|은|가|중|중에서|에서)?\s*(?:최소\s*)?(\d{1,2})\s*명", s)
    m_min = re.search(r"최소\s*(\d{1,2})\s*명|(\d{1,2})\s*명\s*(?:이상|만)\b", s)
    m_pct = re.search(r"(\d{1,3})\s*%\s*이상", s)
    m_frac = re.search(r"(\d)\s*분의\s*(\d)", s)
    if m_others:
        ex.add("quorum", {"kind": "min_count", "count": int(m_others.group(1)),
                          "scope": "others"}, "hard", 3, s)
        got = True
    elif m_min and not re.search(r"시간", s[max(0, m_min.start() - 1): m_min.end() + 1]):
        n = int(m_min.group(1) or m_min.group(2))
        # "재원 포함 2명 이상"처럼 포함 여부를 이미 말했으면 되묻지 않는다.
        needs_scope = (bool(_find_names(s, names)) and bool(re.search(r"반드시|꼭|필수", s))
                       and not re.search(r"포함", s))
        ex.add("quorum", {"kind": "min_count", "count": n, "scope": "total"},
               "hard", 3, s, ["quorum_scope"] if needs_scope else [])
        got = True
    elif m_pct:
        ex.add("quorum", {"kind": "ratio", "ratio": int(m_pct.group(1)) / 100}, "hard", 3, s)
        got = True
    elif m_frac and int(m_frac.group(1)) > 0:
        ex.add("quorum", {"kind": "ratio", "ratio": int(m_frac.group(2)) / int(m_frac.group(1))},
               "hard", 3, s)
        got = True
    elif re.search(r"절반\s*이상", s):
        ex.add("quorum", {"kind": "ratio", "ratio": 0.5}, "hard", 3, s)
        got = True
    elif re.search(r"과반(?:수)?", s):
        ex.add("quorum", {"kind": "majority"}, "hard", 3, s)
        got = True
    elif re.search(r"전원\s*(?:이)?\s*(?:동의|승인)", s):
        ex.add("quorum", {"kind": "all"}, "hard", 3, s)
        got = True
    if got:
        consumed = True
        if re.search(r"가능", s):
            ex.parse_notes.append("'가능하면'은 사람의 동의 기준으로 해석했습니다. 캘린더 "
                                  "가능 여부는 여전히 참가자 전원 기준입니다.")

    # 필수 참석자
    req_names: list[str] = []
    m_list = re.search(r"필수\s*참석자\s*(?:는|은|로|:)?\s*(.+)$", s)
    if m_list:
        tail = m_list.group(1)
        req_names += _find_names(tail, names)
        for tok in re.findall(r"(?<![가-힣])([가-힣]{2,4})(?=\s*(?:,|와|과|랑|이랑|및|$|입니다|이야|야|이다))",
                              tail):
            base = tok[:-1] if len(tok) >= 3 and tok[-1] in "은는이가" else tok
            if base not in names and base not in _STOP_NAMES and tok not in names \
                    and base not in req_names and len(base) >= 2:
                req_names.append(base)
    for clause in split_clauses(s):
        if re.search(r"반드시|꼭|무조건|필수|필참", clause) and re.search(_PART_VERB, clause):
            req_names += _find_names(clause, names)
            req_names += _unknown_name_tokens(clause, names)
    req_names += _required_by_name(s, names)
    # '재원만 꼭 와야 해' / '승현은 선택 참석이야' → 필수 참석자 외에는 응답이 필요 없다
    only = [n for n in names if re.search(
        re.escape(n) + r"\s*만\s*(?:꼭|반드시|무조건)?\s*(?:와야|참석|필수|나와야|있으면|와도)", s)]
    optional_stmt = bool(re.search(r"선택\s*참석|선택적으로|불참해도|참석하지\s*않아도|안\s*와도", s))
    req_names += only
    req_names = list(dict.fromkeys(req_names))
    if req_names:
        ex.add("required_participant", {"names": req_names}, "hard", 3, s)
        consumed = True
    if (only or optional_stmt) and not got:
        ex.add("quorum", {"kind": "required_only"}, "hard", 3, s)
        consumed = True
    return consumed


def _mode(ex: _Extract, s: str) -> bool:
    online = bool(re.search(r"온라인|비대면|화상|줌\b|zoom|구글\s*밋|google\s*meet", s, re.I))
    offline = bool(re.search(r"대면|오프라인|직접\s*(?:만나|보)|얼굴\s*(?:을\s*)?보", s)) \
        and not re.search(r"비대면", s)
    hybrid = bool(re.search(r"혼합|하이브리드|hybrid", s, re.I))
    if not (online or offline or hybrid):
        return False
    hard = bool(re.search(r"반드시|꼭|무조건|만\s*가능|만\s*돼|필수", s))
    if hybrid or (online and offline):
        pref = "in_person" if offline and re.search(r"대면[을를이가은는]?\s*(?:우선|선호)|우선", s) \
            else None
        ex.add("meeting_mode", {"mode": "hybrid", "preferred": pref}, "soft", 3, s)
    elif offline:
        ex.add("meeting_mode", {"mode": "in_person", "preferred": "in_person"},
               "hard" if hard else "soft", 3, s)
    else:
        ex.add("meeting_mode", {"mode": "online", "preferred": None},
               "hard" if hard else "soft", 3, s)
    return True


def _location(ex: _Extract, s: str) -> bool:
    m = re.search(r"(?:장소|위치)\s*(?:는|은|:)\s*([가-힣A-Za-z0-9 ]{2,20})|"
                  r"([가-힣A-Za-z0-9]{2,12})\s*(?:근처|인근|주변)|"
                  r"(카페|사무실|회의실|캠퍼스|학교|도서관|야외)\b", s)
    if not m:
        return False
    text = next(g for g in m.groups() if g).strip()
    ex.add("location", {"text": text}, "soft", 3, s)
    return True


def _recurrence(ex: _Extract, s: str) -> bool:
    if not re.search(r"매주|매월|매달|주마다|격주|정기적|반복(?:해|할|일정|적|되)", s):
        return False
    freq = "biweekly" if "격주" in s else "monthly" if re.search(r"매월|매달", s) else "weekly"
    ex.add("recurrence", {"freq": freq, "same_weekday": bool(re.search(r"같은\s*요일", s))},
           "soft", 3, s)
    return True


def _accessibility(ex: _Extract, s: str) -> bool:
    found = [w for w in ("휠체어", "무장애", "엘리베이터", "경사로", "점자", "수어", "자막",
                         "장애인", "유모차", "접근 가능", "접근가능") if w in s]
    if not found:
        return False
    ex.add("accessibility", {"needs": found}, "hard", 3, s)
    return True


def _external(ex: _Extract, s: str) -> bool:
    if re.search(r"비가|비\s*오|비\s*안|우천|눈이|눈\s*오|날씨|기온|더위|추위|미세먼지|폭염", s):
        soft = bool(re.search(r"좋겠|좋아|가능하면|되도록|웬만하면", s))
        ex.add("external_context", {"kind": "weather", "condition": s[:80]},
               "soft" if soft else "hard", 3, s)
        return True
    if re.search(r"교통|혼잡|러시아워|출퇴근\s*시간|막히", s):
        ex.add("external_context", {"kind": "traffic", "condition": s[:80]}, "soft", 3, s)
        return True
    if re.search(r"영업\s*시간|문\s*(?:을\s*)?(?:여|닫)", s):
        ex.add("external_context", {"kind": "venue_hours", "condition": s[:80]}, "soft", 3, s)
        return True
    return False


def _earliest(ex: _Extract, s: str) -> bool:
    if re.search(r"(?:최대한|가능한\s*한|되도록|가급적)\s*빨리|빠른\s*(?:시간|날짜|시일)|"
                 r"가장\s*빠른|이른\s*시일|\basap\b", s, re.I):
        ex.add("earliest_first", {}, "soft", 4, s)
        return True
    return False
