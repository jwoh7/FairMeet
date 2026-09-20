"""회의 준비 HTTP 엔드포인트. 일정 협상 경로와 독립이며, 업로드 파일은 저장하지 않는다."""
from __future__ import annotations

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import JSONResponse

from common import config
from mediator import llm as llm_mod
from prep import pipeline
from prep.pdf_reader import PdfRejected, read_pdf

router = APIRouter()
_HEADERS = {"Cache-Control": "no-store"}


def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": message}, headers=_HEADERS)


@router.get("/prep/status")
def prep_status():
    """대시보드가 이 기능을 쓸 수 있는지 판단하는 정보. 키·비밀값은 없다."""
    return JSONResponse(headers=_HEADERS, content={
        "enabled": True, "llmConfigured": llm_mod.get_llm() is not None,
        "limits": {"maxBytes": config.PREP_MAX_BYTES, "maxPages": config.PREP_MAX_PAGES,
                   "maxChars": config.PREP_MAX_CHARS, "rounds": config.PREP_ROUNDS,
                   "tokenBudget": config.PREP_TOKEN_BUDGET, "timeoutSec": config.PREP_TIMEOUT_S},
        "model": config.PREP_MODEL or config.LLM_MODEL})


@router.post("/prep/analyze")
def prep_analyze(file: UploadFile = File(...)):
    """PDF 한 개를 올리면 분석·비판·종합 토론 결과를 돌려준다. 결과는 저장하지 않는다."""
    llm = llm_mod.get_llm(model=config.PREP_MODEL)
    if llm is None:
        return _err(503, "LLM 이 설정되어 있지 않아 회의 자료 분석을 쓸 수 없습니다 "
                         "(일정 협상은 그대로 사용할 수 있습니다)")
    # Content-Length 를 믿지 않고 읽는 양 자체를 제한한다
    limit = config.PREP_MAX_BYTES
    data = file.file.read(limit + 1)
    if len(data) > limit:
        return _err(413, f"파일이 너무 큽니다 (최대 {limit // 1024} KB)")
    try:
        doc = read_pdf(data, file.filename or "")
    except PdfRejected as exc:
        return _err(exc.status, exc.message)
    try:
        result = pipeline.analyze(doc, llm)
    except pipeline.PrepUnavailable as exc:
        return _err(exc.status, exc.message)
    return JSONResponse(content=result, headers=_HEADERS)
