"""LLM 없이 하는 제한적인 회의 자료 정리 (규칙 기반).

이것은 다중 에이전트(분석·비판·종합) 분석이 아니다. 문서의 문장을 키워드로 골라 쪽 번호와 함께 그대로
보여 주는 발췌 정리이고, 결과의 meta 에 그렇게 표시한다. 요약을 새로 쓰거나 없던 주장을 만들지 않는다:
모든 항목은 문서에 있는 문장 그대로다. 찬성/반대/위험 분류는 표현(키워드)에 의존하므로 부정확할 수 있다.
"""
from __future__ import annotations

import re

from prep.pdf_reader import PdfDoc

MODE_LABEL = ("규칙 기반의 제한적인 정리예요. AI 다중 에이전트(분석·비판·종합) 분석은 실행되지 않았고, "
              "문서의 문장을 그대로 발췌해 쪽 번호와 함께 보여 드려요.")

_KEY = re.compile(r"(목표|목적|결론|요약|핵심|제안|추진|계획|전략|성과|기대|필요|중요|우선|방안|결정|개선|"
                  r"goal|objective|conclusion|summary|propos|plan\b|strategy|recommend|key\b)", re.I)
_PRO = re.compile(r"(장점|기대|효과|절감|향상|개선|증가|성과|이점|강점|기회|단축|확대|"
                  r"benefit|advantage|reduction|improv|saving|increase|gain)", re.I)
_CON = re.compile(r"(단점|한계|우려|비용|부담|어렵|부족|지연|감소|문제|약점|제약|반대|손실|"
                  r"drawback|limitation|concern|cost|budget|delay|shortfall|downside)", re.I)
_RISK = re.compile(r"(위험|리스크|불확실|가정|미정|TBD|확인\s*필요|검토\s*필요|가능성|위협|취약|전제|"
                   r"risk|uncertain|assum|expires?|not yet|undefined|missing|unknown)", re.I)
_DECIDE = re.compile(r"(결정|승인|합의|선택|채택|여부|논의|확정|의사결정|검토해야|판단|"
                     r"approv|decid|decision|agree|choose|needs? review)", re.I)
_HEADING = re.compile(r"^(?:\d+[.)]|[IVXⅠⅡⅢⅣⅤ]+[.)]|[가-힣][.)]|[■●▶◆□○▪-])\s*\S")
_NUM = re.compile(r"\d")
_END = re.compile(r"[.!?。다요음함]$")
_REMOVED = "[지시로 보이는 문장을 제거함]"


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).casefold()


def _sentences(doc: PdfDoc):
    for pno, page in enumerate(doc.pages, 1):
        for line in page.split("\n"):
            line = line.strip()
            if not line or _REMOVED in line:
                continue
            for s in re.split(r"(?<=[.!?。])\s+", line):
                s = s.strip()
                if 12 <= len(s) <= 300:
                    yield pno, s


def _claim(pno: int, s: str, pages_norm: list[str]) -> dict:
    quote = s[:200]
    ok = _norm(quote) in pages_norm[pno - 1]
    return {"text": s, "evidence": [{"page": pno, "quote": quote, "verified": ok}],
            "verified": ok, "status": "verified" if ok else "unverified"}


def _pick(scored, pattern, limit, used):
    out = []
    for score, pno, s in scored:
        if len(out) >= limit:
            break
        if s in used or not pattern.search(s):
            continue
        used.add(s)
        out.append((pno, s))
    return out


def _headings(doc: PdfDoc, limit: int = 6) -> list[str]:
    seen, out = set(), []
    for page in doc.pages:
        for line in page.split("\n"):
            t = line.strip()
            if not (3 <= len(t) <= 40) or _REMOVED in t or _END.search(t):
                continue
            if _HEADING.match(t) or (t == t.strip() and len(t.split()) <= 6 and _KEY.search(t)):
                key = _norm(t)
                if key not in seen:
                    seen.add(key)
                    out.append(re.sub(r"^(?:\d+[.)]|[IVXⅠⅡⅢⅣⅤ]+[.)]|[가-힣][.)]|[■●▶◆□○▪-])\s*", "", t))
            if len(out) >= limit:
                return out
    return out


def analyze_rules(doc: PdfDoc) -> dict:
    pages_norm = [_norm(p) for p in doc.pages]
    scored, seen = [], set()
    for pno, s in _sentences(doc):
        k = _norm(s)
        if k in seen:
            continue
        seen.add(k)
        score = (2 * len(_KEY.findall(s)) + len(_PRO.findall(s)) + len(_CON.findall(s))
                 + len(_RISK.findall(s)) + (1 if _NUM.search(s) else 0) + (1 if pno == 1 else 0))
        scored.append((score, pno, s))
    scored.sort(key=lambda t: (-t[0], t[1]))
    used: set[str] = set()

    top = [t for t in scored if t[0] > 0][:3] or scored[:3]
    top_by_page = sorted(top, key=lambda t: t[1])
    summary = " ".join(s for _, _, s in top_by_page)
    for _, _, s in top_by_page:
        used.add(s)

    key_claims = [_claim(p, s, pages_norm) for p, s in
                  [(p, s) for _, p, s in top_by_page] +
                  _pick(scored, _KEY, 5, used)]
    pros = [_claim(p, s, pages_norm) for p, s in _pick(scored, _PRO, 4, set())]
    cons = [_claim(p, s, pages_norm) for p, s in _pick(scored, _CON, 4, set())]
    risks = [_claim(p, s, pages_norm) for p, s in _pick(scored, _RISK, 4, set())]
    questions = [f"{s} (p.{p})" for p, s in _pick(scored, _DECIDE, 5, set())]
    agenda = [{"title": h, "minutes": None,
               "purpose": "자료의 소제목이에요. 논의 시간과 목적은 직접 정해 주세요."}
              for h in _headings(doc)]

    warnings = []
    if doc.ignored:
        warnings.append(f"문서 안의 지시로 보이는 문장 {len(doc.ignored)}개는 정리에서 제외했어요")
    warnings += doc.warnings
    warnings.append("찬성·반대·위험 분류는 표현(키워드)으로 고른 것이라 부정확할 수 있어요")
    if not summary:
        warnings.append("문서에서 정리할 만한 문장을 찾지 못했어요")
    return {
        "summary": summary[:1500], "key_claims": key_claims, "pros": pros, "cons": cons,
        "risks_and_gaps": risks, "decision_questions": questions, "suggested_agenda": agenda,
        "consensus": [], "disputes": [], "unverified_claims": [],
        "meta": {"mode": "rules", "modeLabel": MODE_LABEL, "pages": doc.page_count,
                 "characters": doc.chars, "rounds": 0, "calls": 0, "model": None,
                 "ignoredPassages": doc.ignored[:20], "warnings": warnings},
    }
