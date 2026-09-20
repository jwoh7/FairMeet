"""모든 테스트 공통 설정.

  - 실제 LLM 클라이언트가 만들어지지 않게 한다 (필요한 테스트는 FakeLlm 을 직접 주입).
  - 외부 네트워크(실제 API·SMTP·Google) 접근을 막는다. 로컬 연결만 허용.
"""
import pytest

from mediator import llm as llm_mod
from tests import net_guard


@pytest.fixture(autouse=True)
def _isolate_from_the_outside_world():
    net_guard.install()
    llm_mod.set_llm(llm_mod.DISABLED)
    yield
    llm_mod.set_llm(llm_mod.DISABLED)
