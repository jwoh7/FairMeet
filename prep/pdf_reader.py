"""PDF 업로드를 안전하게 텍스트로 바꾼다.

PDF 안의 모든 문장은 '신뢰할 수 없는 입력'이다.
  - PDF 만 받는다 (확장자 + 파일 머리의 %PDF- 서명). 크기·쪽수·추출 글자 수에 상한이 있고,
    넘으면 조용히 잘라 쓰지 않고 거절한다.
  - 손상된 파일, 암호화된 파일, 텍스트가 없는 파일(스캔본)은 이유를 알려 주며 거절한다.
  - 시스템 변경/명령/비밀값 요청, 그리고 'URL 로 접속하라'는 문장은 LLM 에 넘기기 전에
    지우고 사용자에게 알린다. 이 모듈은 URL 에 접속하지 않는다 (네트워크 코드가 없다).
"""
from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field

from common import config
from mediator import nl_rules


class PdfRejected(Exception):
    """업로드를 처리할 수 없는 사유. status 는 HTTP 상태 코드에 대응한다."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class PdfDoc:
    pages: list[str]                                  # 안전하게 정리된 쪽별 텍스트 (1쪽 = pages[0])
    page_count: int
    chars: int
    ignored: list[dict] = field(default_factory=list)  # [{"page": n, "excerpt": "...", "reason": "..."}]
    warnings: list[str] = field(default_factory=list)


_URL = re.compile(r"(?:https?://|ftp://|www\.)\S+", re.I)
_URL_ACTION = re.compile(
    r"\b(?:visit|open|go\s+to|navigate|browse|click|download|fetch|curl|wget|access|connect\s+to)\b|"
    r"접속|방문|열어|열고|다운로드|내려받|클릭|들어가|접근해", re.I)
_SECRET_ASK = re.compile(
    r"(?:비밀번호|패스워드|토큰|api\s*키|시크릿|secret|password|token|api[_\s-]*key|credential)"
    r".{0,20}(?:알려|보내|입력|출력|공유|전송|말해|reveal|send|provide|share|print|leak)|"
    r"(?:알려|보내|입력|출력|공유|전송|말해|reveal|send|provide|share|print|leak).{0,20}"
    r"(?:비밀번호|패스워드|토큰|api\s*키|시크릿|secret|password|token|api[_\s-]*key|credential)",
    re.I)


def _clean(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch if ch in "\n" or unicodedata.category(ch)[0] != "C" else " " for ch in t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def suspicious_reason(sentence: str) -> str | None:
    """이 문장이 '문서 내용'이 아니라 '시스템에 대한 지시'로 보이는 이유. 아니면 None."""
    if nl_rules.detect_injection(sentence):
        return "시스템 변경·명령·비밀값 요청으로 보이는 문장"
    if _SECRET_ASK.search(sentence):
        return "비밀값을 요구하는 문장"
    if _URL.search(sentence) and _URL_ACTION.search(sentence):
        return "URL 접속을 요구하는 문장"
    return None


def sanitize_page(text: str, page_no: int, ignored: list[dict]) -> str:
    """쪽 텍스트에서 지시로 보이는 문장을 지운다 (지운 자리에는 표식만 남긴다)."""
    out = []
    for line in _clean(text).split("\n"):
        pieces = re.split(r"(?<=[.!?。])\s+", line) if line else [""]
        kept = []
        for s in pieces:
            why = suspicious_reason(s) if s else None
            if why:
                ignored.append({"page": page_no, "reason": why, "excerpt": s[:80]})
                kept.append("[지시로 보이는 문장을 제거함]")
            else:
                kept.append(s)
        out.append(" ".join(kept))
    return "\n".join(out).strip()


def read_pdf(data: bytes, filename: str = "", *, max_bytes: int | None = None,
             max_pages: int | None = None, max_chars: int | None = None) -> PdfDoc:
    """업로드된 바이트를 검증하고 쪽별 텍스트로 만든다. 처리할 수 없으면 PdfRejected."""
    max_bytes = max_bytes or config.PREP_MAX_BYTES
    max_pages = max_pages or config.PREP_MAX_PAGES
    max_chars = max_chars or config.PREP_MAX_CHARS

    if filename and not filename.lower().endswith(".pdf"):
        raise PdfRejected(415, "PDF 파일(.pdf)만 업로드할 수 있습니다")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise PdfRejected(400, "빈 파일입니다")
    if len(data) > max_bytes:
        raise PdfRejected(413, f"파일이 너무 큽니다 (최대 {max_bytes // 1024} KB)")
    if b"%PDF-" not in bytes(data[:1024]):
        raise PdfRejected(415, "PDF 파일이 아닙니다 (파일 형식이 맞지 않습니다)")

    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        raise PdfRejected(503, "PDF 처리 라이브러리(pypdf)가 설치되어 있지 않습니다") from None

    try:
        reader = PdfReader(io.BytesIO(bytes(data)), strict=False)
        if reader.is_encrypted:
            raise PdfRejected(422, "암호화된 PDF 는 지원하지 않습니다. 암호를 해제한 파일을 올려 주세요")
        n = len(reader.pages)
    except PdfRejected:
        raise
    except PdfReadError:
        raise PdfRejected(422, "손상되었거나 읽을 수 없는 PDF 입니다") from None
    except Exception:                                        # noqa: BLE001 — 파서 내부 오류도 거절로
        raise PdfRejected(422, "손상되었거나 읽을 수 없는 PDF 입니다") from None
    if n == 0:
        raise PdfRejected(422, "쪽이 없는 PDF 입니다")
    if n > max_pages:
        raise PdfRejected(422, f"쪽수가 너무 많습니다 (최대 {max_pages}쪽, 현재 {n}쪽)")

    pages: list[str] = []
    ignored: list[dict] = []
    warnings: list[str] = []
    for i in range(n):
        try:
            raw = reader.pages[i].extract_text() or ""
        except Exception:                                    # noqa: BLE001
            raw = ""
            warnings.append(f"{i + 1}쪽의 텍스트를 읽지 못했습니다")
        pages.append(sanitize_page(raw, i + 1, ignored))

    total = sum(len(p) for p in pages)
    if total < 30:
        raise PdfRejected(422, "텍스트를 추출할 수 없는 PDF 입니다 (스캔한 이미지 등). "
                               "OCR 은 지원하지 않습니다")
    if total > max_chars:
        raise PdfRejected(422, f"문서가 너무 깁니다 (추출 글자 수 {total:,} > {max_chars:,}). "
                               "일부만 잘라 쓰지 않으므로 더 짧은 자료를 올려 주세요")
    empty = [i + 1 for i, p in enumerate(pages) if not p]
    if empty:
        warnings.append(f"텍스트가 없는 쪽이 있습니다: {', '.join(map(str, empty[:10]))}쪽")
    return PdfDoc(pages=pages, page_count=n, chars=total, ignored=ignored, warnings=warnings)
