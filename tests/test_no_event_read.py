"""애플리케이션 수준 통제의 정적 검증.

'API 권한 수준에서 제목을 읽을 수 없다'는 주장은 사실이 아니다 —
calendar.events.owned 는 소유 캘린더의 이벤트를 읽을 수 있다.

정확한 주장은 이것이다:
    "일정 종류(고정/유동/무시)를 나누려고 events().list 를 쓰지만, Google 의 `fields` 부분 응답으로
     start, end, colorId, transparency, eventType, status 만 요청한다. 제목·설명·장소·참석자·화상회의·첨부는
     요청하지 않아 이 프로세스에 도착하지 않는다. get / instances / watch 는 어디서도 부르지 않는다."

이 테스트가 그 검증이다. 시연 중 심사위원에게 보여줄 수 있다.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# events() 뒤에 붙으면 안 되는 메서드 — 일정 내용을 읽는다 (list 는 아래에서 따로 제한한다)
FORBIDDEN_EVENT_METHODS = {"get", "instances", "watch"}

# 일정 내용을 담는 필드. 부분 응답(fields)에 나타나면 안 된다.
FORBIDDEN_FIELDS = ("summary", "description", "location", "attendees", "conferenceData", "attachments",
                    "creator", "organizer", "htmlLink", "hangoutLink", "extendedProperties", "source",
                    "gadget", "reminders", "id", "iCalUID", "etag", "recurringEventId", "*")

# 검사 대상: 캘린더에 접근하는 모든 코드
SCAN_DIRS = ["agent", "mediator", "slackbot", "prep", "scripts"]


def _iter_python_files():
    for d in SCAN_DIRS:
        p = ROOT / d
        if p.exists():
            yield from p.rglob("*.py")


def _events_calls(method):
    """events().<method>(...) 호출 노드를 (파일, 노드) 로 돌려준다."""
    out = []
    for py in _iter_python_files():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            f = getattr(node, "func", None)
            if (isinstance(node, ast.Call) and isinstance(f, ast.Attribute) and f.attr == method
                    and isinstance(f.value, ast.Call) and isinstance(f.value.func, ast.Attribute)
                    and f.value.func.attr == "events"):
                out.append((py, node))
    return out


def test_no_event_content_read_calls():
    """events().get / instances / watch 호출이 없는지."""
    offenders = [f"{py.relative_to(ROOT)}:{n.lineno} events().{n.func.attr}()"
                 for m in FORBIDDEN_EVENT_METHODS for py, n in _events_calls(m)]
    assert not offenders, "일정 내용을 읽는 호출이 발견되었습니다:\n  " + "\n  ".join(offenders)


def test_events_list_is_only_called_in_the_calendar_client_with_partial_fields():
    calls = _events_calls("list")
    assert calls, "events().list 호출을 찾지 못함 (일정 종류 분류가 동작하지 않는다)"
    for py, node in calls:
        if py.name == "calendar_client.py" and py.parent.name == "agent":
            continue
        # 예외: 시연 일정을 복원하는 오프라인 관리 스크립트는 자기가 만든 일정의 id·색상·한가함만 확인한다
        assert py.name == "reset_demo_calendars.py" and py.parent.name == "scripts", (
            f"{py.relative_to(ROOT)} 에서 events().list 를 부르면 안 된다")
        import re
        m = re.search(r'fields\s*=\s*"items\(([^)]*)\)"', py.read_text(encoding="utf-8"))
        assert m and set(m.group(1).split(",")) <= {"id", "colorId", "transparency"}, "부분 응답 필드가 너무 넓다"
    src = (ROOT / "agent" / "calendar_client.py").read_text(encoding="utf-8")
    assert "fields=EVENT_FIELDS" in src and "q=" not in src.split("def fetch_events")[1].split("def _parse_event")[0]


def test_the_partial_response_asks_only_for_classification_fields():
    from agent.calendar_client import EVENT_FIELDS
    body = EVENT_FIELDS.replace("nextPageToken,", "")
    assert body.startswith("items(") and body.endswith(")")
    asked = set(body[len("items("):-1].split(","))
    assert asked == {"start", "end", "colorId", "transparency", "eventType", "status"}
    for bad in FORBIDDEN_FIELDS:
        assert bad not in asked, bad


def test_scopes_are_not_widened():
    """읽기 전용 광범위 스코프(calendar.readonly)나 전체 캘린더 스코프를 요청하지 않는지."""
    from agent.calendar_client import SCOPES
    src = (ROOT / "agent" / "calendar_client.py").read_text(encoding="utf-8")
    assert "auth/calendar.readonly" not in src
    assert sorted(SCOPES) == ["https://www.googleapis.com/auth/calendar.events.owned",
                              "https://www.googleapis.com/auth/calendar.freebusy"]

def test_no_event_titles_in_cost_module():
    """비용 함수가 일정 제목을 참조하지 않는지 (FreeBusy 는 애초에 안 주지만)."""
    src = (ROOT / "agent" / "cost.py").read_text(encoding="utf-8")
    for bad in ['["summary"]', '["description"]', '["location"]', '.summary']:
        assert bad not in src, f"cost.py 가 {bad} 를 참조함"


def test_protocol_has_no_reason_field():
    """거절 사유를 담을 통로가 스키마에 없는지."""
    src = (ROOT / "common" / "protocol.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Response":
            fields = {n.target.id for n in node.body
                      if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
            assert "reason" not in fields, "Response 에 reason 필드가 생겼습니다"
            return
    raise AssertionError("Response 클래스를 찾지 못함")
