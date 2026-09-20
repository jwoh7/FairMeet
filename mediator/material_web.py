"""회의 자료 정리 열람 페이지. 이메일의 서명된 개인 링크로만 들어온다 (휴대폰에서도 읽을 수 있다).

링크가 열리지 않는 이유(서명 불일치, 만료, 권한 회수, 회의 취소, 참석자가 아님 ...)는 구분하지 않고 항상 같은
문구만 보여 준다. 다른 참가자의 이메일이나 수신 여부는 어디에도 나오지 않는다. GET 은 아무것도 바꾸지 않는다.
"""
from __future__ import annotations

import html

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from mediator import materials
from mediator.approval_web import _page

router = APIRouter()
esc = html.escape


def _unavailable() -> HTMLResponse:
    r = _page('<p class="big">열람할 수 없는 링크예요</p>'
              '<p class="note">이 링크로는 자료를 볼 수 없어요. 링크가 만료됐거나, 이 회의의 참석자가 아니게 됐거나, '
              '회의가 변경·취소됐을 수 있어요. 주최자에게 문의해 주세요.</p>', "FairMeet 회의 자료")
    r.status_code = 404
    return r


def _claims(title: str, items) -> str:
    if not items:
        return ""
    out = [f"<h2>{esc(title)}</h2>"]
    for c in items:
        ev = "".join(
            f'<span class="pg">p.{esc(str(e.get("page") or "?"))}'
            f'{"" if e.get("verified") else " · 문서에서 확인되지 않음"}</span>'
            for e in c.get("evidence", []))
        unv = "" if c.get("verified") else '<span class="pg">⚠ 근거 미확인</span>'
        out.append(f'<div class="claim{"" if c.get("verified") else " unv"}">{esc(c.get("text", ""))}{ev}{unv}</div>')
    return "".join(out)


def _strings(title: str, items) -> str:
    if not items:
        return ""
    return f"<h2>{esc(title)}</h2>" + "".join(f'<div class="claim">{esc(str(t))}</div>' for t in items)


def _agenda(items) -> str:
    if not items:
        return ""
    rows = []
    for a in items:
        m = f" · {esc(str(a['minutes']))}분" if a.get("minutes") else ""
        rows.append(f'<div class="claim"><strong>{esc(a.get("title", ""))}</strong>{m}'
                    f'<br><span class="note">{esc(a.get("purpose", ""))}</span></div>')
    return "<h2>추천 안건</h2>" + "".join(rows)


@router.get("/materials/{token}", response_class=HTMLResponse)
def view_material(token: str):
    v = materials.inspect_token(token)
    if v is None:
        return _unavailable()
    r, meta = v.result, v.result.get("meta") or {}
    if v.mode == "rules":
        banner = (f'<div class="alert warn"><strong>제한적인 정리예요.</strong> '
                  f'{esc(meta.get("modeLabel", ""))}</div>')
    else:
        banner = ('<div class="alert">AI가 정리한 결과예요. 문서에서 근거를 확인하지 못한 주장은 '
                  '‘근거 미확인’으로 표시돼 있어요.</div>')
    unverified = _strings("문서에서 확인되지 않은 주장", r.get("unverified_claims"))
    body = f"""
<h1>회의 자료 정리</h1>
<div class="slot">{esc(v.title)}<br><span class="note">{esc(v.slot_text)}</span></div>
<p class="note">{esc(v.display_name)} 님, 이 회의에 참석하기로 하신 분께만 공유되는 자료예요.</p>
{banner}
<h2>핵심 요약</h2><div class="claim">{esc(r.get("summary") or "요약을 만들지 못했어요.")}</div>
{_claims("주요 쟁점", r.get("key_claims"))}
{_claims("찬성 근거", r.get("pros"))}
{_claims("반대 근거", r.get("cons"))}
{_claims("위험과 부족한 정보", r.get("risks_and_gaps"))}
{_strings("결정할 안건", r.get("decision_questions"))}
{_agenda(r.get("suggested_agenda"))}
{_strings("합의된 점", r.get("consensus"))}{_strings("의견이 갈리는 점", r.get("disputes"))}
{unverified}
<hr>
<p class="note">이 링크는 {v.expires_at:%m월 %d일 %H:%M} 까지 열 수 있고, 참석 상태가 바뀌면 열 수 없게 돼요.
다른 사람에게 전달하지 마세요. 원본 PDF는 이 페이지에 없어요.</p>"""
    return _page(body, "FairMeet 회의 자료")
