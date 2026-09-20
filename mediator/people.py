"""회의 참가자 선택: 문장에서 누구와 회의하는지 뽑고, 등록된 참가자와 대조해 확정한다.

  재원, 도연과 회의            → 재원·도연만 (정확히 두 명)
  재원, 도연 두 명과만 회의     → 재원·도연만. '두 명'은 이름 수와 대조한다
  승현도 포함                  → 기존 선택에 승현 추가
  도연은 빼고                  → 도연 제외
  모두와                       → 등록된 전체
  재원 포함 최소 2명            → 참가자 선택이 아니라 승인 기준(재원 필수, 정족수 2)이다
  (아무 말도 없음)              → 기본값: 등록된 전체 (사용자가 확인해야 시작할 수 있다)

원칙
  - 이름은 등록된 참가자 목록과 원문 둘 다에 있어야 한다. 원문에 없는 이름(LLM 이 만든 것 포함)은 버린다.
  - 등록되지 않은 이름은 만들지 않고 막는다 (unknown_participant). 이름이 겹치면 추측하지 않고 직접 고르게 한다.
  - 주최자(회의를 여는 사람)는 문장에 '두 명만'이 있으면 자동으로 넣지 않는다. 회의 이벤트를 만드는 '캘린더 주인'은
    선택된 사람 중 주최자 프로필이 있으면 그 사람, 없으면 목록의 첫 사람이다 (화면에 표시된다).
    이름이 하나뿐이면 혼자 하는 회의가 되므로 주최자를 함께 넣고 그 사실을 알린다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from mediator import nl_rules
from mediator.request_model import Issue

_JOSA = nl_rules._JOSA

_CUE = re.compile(r"회의|미팅|만나|만남|약속|모임|스터디|면담|리뷰|워크\s*숍|워크\s*샵|발표|식사|점심|커피|통화|"
                  r"잡(?:아|고|을|자|았)|함께|같이|모이")
_COUNT = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6}
_COUNT_RE = re.compile(r"(?<!최소\s)(?<!최소)(한|두|세|네|다섯|여섯|\d{1,2})\s*명\s*"
                       r"(?:과|와|랑|이랑|하고|만|이서|끼리|으로)")
# '재원, 도연, 승현 중 2명만 참여해도 되는' 은 참가자를 2명만 고르라는 뜻이 아니라 승인 정족수 표현이다.
# 이런 문맥에서는 인원 수를 '참가자 수'로 읽지 않는다 (정족수는 nl_rules 가 따로 읽는다).
_OUT_OF_RE = re.compile(r"(?:중|중에서)\s*(?:최소\s*)?(?:한|두|세|네|다섯|여섯|\d{1,2})\s*명")
_ENOUGH_RE = re.compile(r"(?:한|두|세|네|다섯|여섯|\d{1,2})\s*명\s*(?:만|이상)?\s*"
                        r"(?:[가-힣]{0,6}\s*)?(?:해도|하면)\s*(?:되|돼|충분|진행|확정)")
_ALL_RE = re.compile(r"(?:모두|전원|다\s*같이|다같이|전체|다\s*함께)\s*(?:와|과|랑|이랑|하고|함께|같이)|"
                     r"등록된\s*(?:모든|전체)|모든\s*(?:참가자|팀원|멤버)\s*(?:와|과|랑|하고|이랑|함께|같이)")
_TIMEISH = re.compile(r".*(?:요일|시|분|주|월|일|간|후|전|때|오전|오후|저녁|아침|점심|내일|모레|오늘|이번|다음|주말)$")
_STOP = {"우리", "모두", "전원", "팀원", "참석자", "참가자", "다들", "서로", "각자", "저희", "누구"}


def _find_names(text: str, names: list[str]) -> list[str]:
    seen, out = set(), []
    for n in nl_rules._find_names(text, names):
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _has(pattern: str, name: str, s: str) -> bool:
    return re.search(pattern.replace("{N}", re.escape(name)), s) is not None


def _required_ctx(name: str, s: str) -> bool:
    """이름이 '필수 참석자' 문맥에서만 나왔는가 (참가자 선택이 아니라 필수 지정)."""
    return (_has(r"(?<![가-힣]){N}\s*(?:은|는|이|가|만|도)?\s*(?:반드시|꼭|무조건|필수|필참)", name, s)
            or _has(r"필수\s*참석자\s*(?:는|은|로|:)?[^.]*(?<![가-힣]){N}", name, s))


def _quorum_ctx(name: str, s: str) -> bool:
    """'재원 포함 2명 이상' 처럼 승인 기준을 말하는 문맥."""
    return _has(r"(?<![가-힣]){N}\s*(?:을|를)?\s*포함(?:해서|하여|하고|한|해)?\s*(?:최소\s*)?(?:\d|한|두|세|네|다섯|과반|절반)", name, s)


def extract(sentences: list[str], names: list[str]) -> dict:
    """문장에서 참가자 관련 표현을 뽑는다 (규칙 기반, 외부 호출 없음).

    Returns: {"listed", "added", "removed", "all", "count", "unknown", "source_text"}
    """
    listed: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    unknown: list[str] = []
    everyone, count, src = False, None, None

    for s in sentences:
        # 제외 / 추가 표현은 회의 문장이 아니어도 읽는다 ("도연은 빼고", "승현도 포함")
        gone_here, add_here = set(), set()
        for n in names:
            if _has(r"(?<![가-힣]){N}\s*(?:은|는|이|가|도|만)?\s*(?:빼고|빼줘|빼\s*주|빼|제외|없이|말고|불참)", n, s):
                gone_here.add(n)
            if _has(r"(?<![가-힣]){N}\s*(?:도|까지)\s*(?:포함|추가|넣|초대|같이|함께)", n, s):
                add_here.add(n)
        removed += [n for n in names if n in gone_here and n not in removed]
        added += [n for n in names if n in add_here and n not in added]

        cue = _CUE.search(s) is not None
        m_count = _COUNT_RE.search(s)
        if not (cue or m_count):
            continue
        if _ALL_RE.search(s):
            everyone, src = True, src or s
        if m_count and count is None and not (_OUT_OF_RE.search(s) or _ENOUGH_RE.search(s)):
            g = m_count.group(1)
            count = _COUNT.get(g, int(g) if g.isdigit() else None)

        for n in _find_names(s, names):
            if n in gone_here or n in add_here or _required_ctx(n, s) or _quorum_ctx(n, s):
                continue
            if n not in listed:
                listed.append(n)
                src = src or s

        # 목록에 없는 이름 후보: '<이름>와/과/랑/하고 … 회의' 꼴에서만 본다 (시간 표현 등은 거른다)
        for m in re.finditer(r"(?<![가-힣])([가-힣]{2,3})\s*(?:이랑|랑|과|와|하고)\s*(?:[^.\n]{0,14}?)"
                             r"(?:회의|미팅|만나|약속|모임)", s):
            tok = m.group(1)
            if tok in names or tok in _STOP or _TIMEISH.match(tok) or tok in unknown:
                continue
            if any(tok.startswith(n) for n in names):          # '재원이랑' 처럼 조사가 붙은 알려진 이름
                continue
            unknown.append(tok)

        # 쉼표로 이어진 목록의 이름 후보 ("민수, 도연과 회의")
        for m in re.finditer(r"((?:[가-힣]{2,3}\s*[,·]\s*)+[가-힣]{2,3})\s*(?:이랑|랑|과|와|하고)?\s*(?:[^.\n]{0,14}?)"
                             r"(?:회의|미팅|만나|약속|모임)", s):
            for tok in re.split(r"\s*[,·]\s*", m.group(1)):
                if tok in names or tok in _STOP or _TIMEISH.match(tok) or tok in unknown:
                    continue
                if any(tok.startswith(n) for n in names):
                    continue
                unknown.append(tok)

    return {"listed": listed, "added": added, "removed": removed, "all": everyone, "count": count,
            "unknown": unknown, "source_text": src}


@dataclass
class PeopleResult:
    ids: list[str]
    source: str                                       # text | default
    notes: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


def resolve(spec: dict | None, participants: list[dict]) -> PeopleResult:
    """뽑은 표현을 등록된 참가자와 대조해 최종 선택을 정한다."""
    everyone = [p["agent_id"] for p in participants]
    by_name: dict[str, list[dict]] = {}
    for p in participants:
        by_name.setdefault(p["name"], []).append(p)
    organizer = next((p["agent_id"] for p in participants if p.get("is_organizer")), None)
    issues: list[Issue] = []
    notes: list[str] = []
    spec = spec or {}

    def pick(names: list[str]) -> list[str]:
        out = []
        for n in names:
            found = by_name.get(n, [])
            if not found:
                continue                                  # 등록되지 않은 이름은 여기서 만들지 않는다
            if len(found) > 1:
                issues.append(Issue("unknown_participant",
                                    f"'{n}' 이름의 참가자가 여러 명이라 누구인지 알 수 없어요. "
                                    "참가자 선택에서 직접 골라 주세요."))
                continue
            out.append(found[0]["agent_id"])
        return out

    for tok in spec.get("unknown", []):
        issues.append(Issue("unknown_participant",
                            f"'{tok}'은(는) 등록된 참가자가 아니에요. 등록된 참가자: "
                            f"{', '.join(by_name)}"))

    listed, added, removed = spec.get("listed", []), spec.get("added", []), spec.get("removed", [])
    evidence = bool(listed or added or removed or spec.get("all"))
    if listed:
        ids = pick(listed)
    else:
        ids = list(everyone)
    for i in pick(added):
        if i not in ids:
            ids.append(i)
    gone = set(pick(removed))
    ids = [i for i in ids if i not in gone]
    order = {i: k for k, i in enumerate(everyone)}
    ids.sort(key=lambda i: order.get(i, 0))

    if len(ids) == 1 and organizer and ids[0] != organizer and evidence:
        ids.append(organizer)
        ids.sort(key=lambda i: order.get(i, 0))
        name = next(p["name"] for p in participants if p["agent_id"] == organizer)
        notes.append(f"혼자 하는 회의가 되지 않도록 주최자({name})를 함께 넣었어요. 필요 없으면 참가자 선택에서 바꿔 주세요.")
    count = spec.get("count")
    if count is not None and listed and len(ids) != count:
        issues.append(Issue("conflict", f"'{count}명'이라고 하셨는데 선택된 참가자는 {len(ids)}명이에요. "
                                        "참가자를 확인해 주세요."))
    return PeopleResult(ids, "text" if evidence else "default", notes, issues)


# ---- 선택 · 캘린더 주인 -------------------------------------------------------------------------------

def select(participants: list[dict], ids: list[str] | None) -> list[dict]:
    """등록된 참가자 중 ids 만 골라 세션 스냅샷용 목록을 만든다 (원본은 바꾸지 않는다).

    캘린더 주인(회의 이벤트를 만드는 사람)은 정확히 한 명만 표시한다: 선택된 사람 중 주최자 프로필이 있으면 그 사람,
    없으면 목록의 첫 사람이다. ids 가 None 이면 전체."""
    chosen = [dict(p) for p in participants if ids is None or p["agent_id"] in set(ids)]
    if not chosen:
        return []
    host = next((p for p in chosen if p.get("is_organizer")), chosen[0])
    for p in chosen:
        p["is_organizer"] = p is host
    return chosen


def host_of(selected: list[dict]) -> dict | None:
    return next((p for p in selected if p.get("is_organizer")), None)
