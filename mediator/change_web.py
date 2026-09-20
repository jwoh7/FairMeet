"""일정 변경 요청·투표 페이지. 서명된 일회용 링크로만 들어온다.

approval_web 과 같은 원칙: GET 은 아무것도 바꾸지 않는다(메일 보안 스캐너가 링크를 미리 열어도
소비되지 않는다), 링크가 열리지 않는 이유는 항상 같은 문구로만 알린다, 요청자의 신원과 사유는
어디에도 나오지 않는다.
"""
from __future__ import annotations

import html

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

from mediator import change_flow
from mediator.approval_web import _done, _page, _stale

router = APIRouter()

_KIND_INTRO = {
    "renegotiate": ("모두의 새 시간을 다시 찾는 것", "동의하면 새 시간 제안 이메일이 다시 발송됩니다. "
                    "새 시간이 확정되기 전까지 기존 일정은 그대로 유지되고, 새 시간을 찾지 못하면 "
                    "기존 일정이 유지됩니다."),
    "proceed_without": ("참가자 한 분을 제외하고 기존 시간에 진행하는 것",
                        "동의하면 그분만 캘린더 초대에서 빠지고 회의는 기존 시간에 진행됩니다."),
    "cancel": ("회의를 취소하는 것", "동의하면 캘린더 이벤트가 삭제되고 참석자에게 취소가 안내됩니다."),
}


@router.get("/change/request/{token}", response_class=HTMLResponse)
def show_request(token: str):
    view = change_flow.inspect_request_token(token)
    if view is None:
        return _stale()
    return _page(f"""
<h1>확정된 회의</h1>
<div class="slot">{html.escape(view.slot_text)}</div>
<p>{html.escape(view.display_name)} 님, 이 회의에 변경이 필요하신가요?</p>
<p class="note">요청을 보내도 <strong>일정은 바뀌지 않습니다.</strong> 주최자가 확인한 뒤 필요한 경우에만
다시 안내드려요. 이유를 적을 필요가 없고, 요청하신 분이 누구인지도 다른 참가자에게 알리지 않습니다.</p>
<form method="post" class="actions col">
  <button class="ok" type="submit">일정 변경 요청 보내기</button>
</form>
<hr>
<p class="note">이 링크는 한 번만 사용할 수 있습니다. 요청이 접수되면 소비됩니다.</p>""",
                 "FairMeet 일정 변경 요청")


@router.post("/change/request/{token}", response_class=HTMLResponse)
def submit_request(token: str):
    """참가자는 변경 요청만 보낸다. 처리 방법은 주최자가 관리자 화면에서 고른다."""
    result = change_flow.submit_intake(token)
    if result.status == "invalid":
        return _stale()
    if result.status == "rejected":
        return _done("변경을 요청할 수 없습니다", html.escape(result.message))
    return _done("변경 요청을 보냈어요",
                 "주최자가 확인한 뒤 필요한 경우 다시 안내드릴게요. 그 전까지 일정은 그대로 유지됩니다.")


@router.get("/change/vote/{token}", response_class=HTMLResponse)
def show_vote(token: str):
    view = change_flow.inspect_vote_token(token)
    if view is None:
        return _stale()
    what, detail = _KIND_INTRO[view.kind]
    return _page(f"""
<h1>일정 변경 요청</h1>
<div class="slot">{html.escape(view.slot_text)}</div>
<p>{html.escape(view.display_name)} 님, 참가자 한 분이 이 회의에 대해
<strong>{html.escape(what)}</strong>을(를) 요청했습니다. 동의하시나요?</p>
<p class="note">{html.escape(detail)}</p>
<p class="note">{html.escape(view.rule_text)}</p>
<form method="post" class="actions">
  <button class="ok" name="verdict" value="approve" type="submit">동의합니다</button>
  <button class="no" name="verdict" value="reject" type="submit">거절합니다</button>
</form>
<hr>
<p class="note">이 링크는 {view.expires_at:%m월 %d일 %H:%M} 까지 유효하며 한 번만 사용할 수 있습니다.<br>
요청한 분이 누구인지, 다른 참가자가 어떻게 응답했는지는 표시되지 않습니다.
투표가 통과되기 전까지 기존 일정은 그대로입니다.</p>""", "FairMeet 일정 변경 투표")


@router.post("/change/vote/{token}", response_class=HTMLResponse)
def submit_vote(token: str, verdict: str = Form(...)):
    if verdict not in ("approve", "reject"):
        return _stale()
    result = change_flow.decide_vote(token, verdict)
    if result.status == "invalid":
        return _stale()
    change_flow.run_vote_hooks(result)
    if result.status == "accepted":
        return _done("변경이 승인되었습니다", "곧 요청한 변경이 적용됩니다. 결과는 이메일로 알려 드립니다.")
    if result.status == "denied":
        return _done("응답이 접수되었습니다", "이번 변경은 받아들여지지 않아 기존 일정이 유지됩니다.")
    return _done("응답이 접수되었습니다",
                 "다른 참가자의 응답을 기다리는 중입니다. 누가 아직 응답하지 않았는지는 표시하지 않습니다.")
