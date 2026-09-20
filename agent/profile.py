"""사용자 프로필. 계정을 연결하는 데 필요한 최소 정보만 담는다.

개인 취향(아침형/저녁형), 보호 시간, 하루 일정 허용량, 캘린더 분류 같은 것은 이 파일(JSON)에 적지 않는다.
일정의 종류는 사용자가 Google Calendar 에서 색상과 '바쁨/한가함'으로 표시하고, 어떤 색상이 고정·유동·무시인지는
사용자가 설정 화면에서 직접 고른다 (agent/calendar_settings.py). 옛 프로필 JSON 에 남아 있는 취향 키는
읽을 때 버리며 비용 계산에 전혀 쓰이지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Profile:
    """한 사용자의 연결 정보. 이 객체는 절대 네트워크로 나가지 않는다."""
    user_key: str                      # 가명 생성용 (이메일 등, 외부 전송 안 함)
    display_name: str
    email: str                         # attendee 초대용
    token_path: str = ""               # Google OAuth 토큰 파일
    is_organizer: bool = False         # 회의 이벤트를 만드는 캘린더 주인이 될 수 있는가

    @staticmethod
    def load(path: str | Path) -> "Profile":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f.name for f in fields(Profile)}
        return Profile(**{k: v for k, v in data.items() if k in known})

    def save(self, path: str | Path) -> None:
        from dataclasses import asdict
        Path(path).write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8")


def validate_profile(profile: Profile) -> list[str]:
    """프로필 로드 시 1회 검증. 비어 있지 않으면 협상 참여를 거부한다."""
    errors: list[str] = []
    if not profile.user_key:
        errors.append("user_key 가 비어 있습니다")
    if not profile.display_name:
        errors.append("display_name 이 비어 있습니다")
    if not profile.email or "@" not in profile.email:
        errors.append(f"이메일 형식 오류: {profile.email!r}")
    return errors
