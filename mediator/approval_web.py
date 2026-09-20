"""참가자별 서명된 1회용 링크로 비공개 승인을 받는다.

공개 채널에 누가 승인했는지 표시하지 않는다. 미승인자를 노출하면
"왜 아직 승인 안 했어?"라는 압박이 그대로 재현되고, 그것이 바로
이 시스템이 없애려는 것이다.

링크가 열리지 않는 이유(서명 불일치, 만료, 이전 제안, 이미 사용, 세션 종료)는
화면에서 구분하지 않고 항상 같은 문구를 보여준다. 이유를 구분해 알려주면
링크 추측이나 다른 참가자의 상태를 알아내는 통로가 되기 때문이다.

이 모듈은 GET 에서는 아무것도 바꾸지 않는다. 메일 보안 스캐너가 링크를 미리
열어 보아도 승인이 소비되지 않도록, 응답은 반드시 POST 로만 받는다.
"""
from __future__ import annotations

import html

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse

from mediator import approval_flow, store
from mediator import policy as policy_mod

router = APIRouter()

STALE_TITLE = "만료되었거나 이미 처리된 요청입니다"

# 링크가 주소창·로그·Referer 로 새거나 캐시에 남지 않게 한다.
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex"}


# FairMeet 자체 로고 (겹치는 두 원: 서로의 시간이 겹치는 곳). 외부 자산이 아니다.
_MARK = ('<svg viewBox="0 0 32 32" aria-hidden="true"><circle cx="12" cy="16" r="9" fill="#0f766e" '
         'opacity=".92"/><circle cx="20" cy="16" r="9" fill="#5eead4" opacity=".85"/></svg>')


def _page(body: str, title: str = "FairMeet") -> HTMLResponse:
    """참가자용 페이지 공통 틀. 휴대폰에서도 쓸 수 있고, 스타일은 /assets/fairmeet.css (CSP: 인라인 스타일 없음)."""
    return HTMLResponse(f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title><link rel="stylesheet" href="/assets/fairmeet.css"></head>
<body class="participant"><main class="wrap"><div class="pbrand">{_MARK}<span>FairMeet</span></div>{body}</main></body></html>""",
                        headers=_HEADERS)


def _done(title: str, body: str) -> HTMLResponse:
    return _page(f'<p class="big">{html.escape(title)}</p>'
                 f'<p class="note">{body}</p>', title)


def _stale() -> HTMLResponse:
    """모든 무효 사유에 대해 같은 화면. 아무것도 처리하지 않았다는 뜻이다."""
    return _done(STALE_TITLE,
                 "이 링크로는 더 이상 응답할 수 없습니다. "
                 "새로운 요청이 있다면 가장 최근에 받은 이메일의 링크를 이용해 주세요.")


@router.get("/approve/{token}", response_class=HTMLResponse)
def show(token: str):
    view = approval_flow.inspect_token(token)
    if view is None:
        return _stale()

    replaces = ("<p class=\"note\">앞서 안내드린 시간을 대체하는 새 제안입니다.</p>"
                if view.proposal_version > 1 else "")
    policy = ""
    if view.role_text or view.policy_text:
        policy = (f'<p class="note"><strong>{html.escape(view.role_text)}</strong><br>'
                  f'{html.escape(view.policy_text)}</p>')
    return _page(f"""
<h1>제안된 시간</h1>
<div class="slot">{html.escape(view.slot_text)}</div>
{replaces}{policy}
<p>{html.escape(view.display_name)} 님, 이 시간에 동의하시나요?</p>
<p class="note">동의하지 않으셔도 <strong>이유를 설명할 필요가 없습니다.</strong>
거절하면 에이전트가 다른 시간을 찾습니다.</p>
<form method="post" class="actions">
  <button class="ok" name="verdict" value="approve" type="submit">동의합니다</button>
  <button class="no" name="verdict" value="reject" type="submit">거절합니다</button>
</form>
<hr>
<p class="note">이 링크는 {view.expires_at:%m월 %d일 %H:%M} 까지 유효하며 한 번만 사용할 수 있습니다.<br>
다른 참가자가 어떻게 응답했는지, 누가 아직 응답하지 않았는지는
표시되지 않습니다.</p>""", "FairMeet 승인")


@router.post("/approve/{token}", response_class=HTMLResponse)
def decide(token: str, verdict: str = Form(...)):
    if verdict not in ("approve", "reject"):
        raise HTTPException(400, "잘못된 값")

    result = approval_flow.decide_with_token(token, verdict)
    if result.status == "invalid":
        return _stale()

    # 최종 커밋·새 제안 이메일은 응답을 기다리지 않고 백그라운드로 진행된다.
    approval_flow.run_hooks(result)

    if verdict == "approve":
        if result.status == "all_approved":
            row = store.get_session(result.session_id)
            quorum = (row is not None and
                      policy_mod.ApprovalPolicy.from_row(row).mode != "all")
            if quorum:
                return _done("확정 기준을 충족했습니다",
                             "동의한 참가자의 캘린더를 다시 확인한 뒤 일정을 등록합니다.")
            return _done("전원 동의 완료",
                         "모든 참가자의 캘린더를 다시 확인한 뒤 일정을 등록합니다.")
        return _done("동의가 기록되었습니다",
                     "다른 참가자의 응답을 기다리는 중입니다. "
                     "누가 아직 응답하지 않았는지는 표시하지 않습니다.")

    if result.status == "terminated":
        return _done("거절이 접수되었습니다",
                     "이유는 다른 참가자에게 전달되지 않습니다. "
                     "더 제안할 수 있는 시간이 없어 이번 협상은 종료되었습니다.")
    if result.status == "all_approved":
        # 선택 참석자의 거절이 마지막 응답이었고, 남은 동의만으로 확정 기준을 채운 경우
        return _done("거절이 접수되었습니다 · 확정 기준을 충족했습니다",
                     "이유는 다른 참가자에게 전달되지 않습니다. "
                     "동의한 참가자만으로 기준을 채워 일정이 등록됩니다.")
    if result.status == "recorded":
        # 선택 참석자의 거절이라 확정 기준에는 영향이 없는 경우
        return _done("거절이 접수되었습니다",
                     "이유는 다른 참가자에게 전달되지 않습니다. "
                     "다른 참가자의 응답으로 확정 여부가 정해집니다.")
    return _done("거절이 접수되었습니다",
                 "이유는 다른 참가자에게 전달되지 않습니다. "
                 "에이전트가 다른 시간을 찾아 새로운 요청을 보냅니다.")
