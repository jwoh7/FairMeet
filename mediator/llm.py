"""선택형 LLM 어댑터. 자연어 회의 조건 해석과 회의 자료 분석이 함께 쓴다.

원칙
  - LLM 은 '제안'만 한다. 출력은 항상 호출자가 결정론적으로 다시 검증한다.
  - LLM 이 없거나 실패해도 일정 기능은 그대로 동작한다. 실패는 LlmError 로 올라오고
    호출자는 규칙 기반 경로로 되돌아가거나 기능을 끈다.
  - 자연어를 코드·SQL·셸로 실행하지 않는다. 모델에는 도구를 주지 않고 JSON 만 받는다.
  - 오류 메시지에는 키나 요청 본문을 싣지 않는다.
  - 테스트는 실제 API 를 부르지 않는다: set_llm(FakeLlm(...)) 또는 set_llm(DISABLED).

Claude API 사용법은 공식 Python SDK 의 `messages.create` + `output_config.format`
(JSON 스키마) 규격을 따른다. SDK 는 실제로 필요할 때만 import 한다.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol

from common import config

log = logging.getLogger("fairmeet.llm")


class LlmError(RuntimeError):
    """LLM 호출/응답 실패. 메시지는 사용자에게 보여도 안전하다(비밀값 없음)."""


class LlmClient(Protocol):
    name: str

    def complete_json(self, *, system: str, user: str, schema: dict,
                      max_tokens: int | None = None) -> dict: ...


class _Disabled:
    name = "disabled"


DISABLED = _Disabled()          # 테스트가 실제 클라이언트가 만들어지지 않게 하는 표식


class AnthropicLlm:
    """Anthropic Messages API. 구조화 출력(JSON 스키마)으로 JSON 객체 하나만 받는다."""

    name = "anthropic"

    def __init__(self, model: str | None = None, timeout_s: float | None = None,
                 client: Any = None):
        self.model = model or config.LLM_MODEL
        self.timeout_s = timeout_s if timeout_s is not None else config.LLM_TIMEOUT_S
        self._client = client
        self.tokens_used = 0              # 이 인스턴스가 지금까지 쓴 입력+출력 토큰 (응답 usage 기준)

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise LlmError("anthropic 패키지가 설치되어 있지 않습니다") from None
            self._client = anthropic.Anthropic(timeout=self.timeout_s, max_retries=1)
        return self._client

    def complete_json(self, *, system: str, user: str, schema: dict,
                      max_tokens: int | None = None) -> dict:
        client = self._get_client()
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=max_tokens or config.LLM_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except LlmError:
            raise
        except Exception as exc:                                  # noqa: BLE001
            # SDK 예외 문자열에 요청 정보가 섞일 수 있어 종류만 남긴다.
            raise LlmError(f"LLM 호출에 실패했습니다 ({type(exc).__name__})") from None

        usage = getattr(resp, "usage", None)
        self.tokens_used += int(getattr(usage, "input_tokens", 0) or 0) + int(
            getattr(usage, "output_tokens", 0) or 0)
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            raise LlmError("모델이 이 요청을 처리하지 않았습니다")
        if stop == "max_tokens":
            raise LlmError("모델 응답이 길이 제한으로 잘렸습니다")
        text = next((b.text for b in getattr(resp, "content", []) or []
                     if getattr(b, "type", None) == "text"), None)
        if not text:
            raise LlmError("모델이 빈 응답을 돌려주었습니다")
        try:
            data = json.loads(text)
        except ValueError:
            raise LlmError("모델 응답이 JSON 이 아닙니다") from None
        if not isinstance(data, dict):
            raise LlmError("모델 응답이 JSON 객체가 아닙니다")
        return data


class FakeLlm:
    """테스트용. 네트워크 없이 미리 정한 응답을 돌려주고 호출 내용을 기록한다."""

    name = "fake"

    def __init__(self, response: dict | None = None, error: Exception | None = None,
                 fn=None, tokens_per_call: int = 0):
        self.response = response
        self.error = error
        self.fn = fn
        self.calls: list[dict] = []
        self.tokens_used = 0
        self.tokens_per_call = tokens_per_call

    def complete_json(self, *, system: str, user: str, schema: dict,
                      max_tokens: int | None = None) -> dict:
        self.calls.append({"system": system, "user": user, "schema": schema,
                           "max_tokens": max_tokens})
        self.tokens_used += self.tokens_per_call
        if self.error is not None:
            raise self.error
        if self.fn is not None:
            return self.fn(system=system, user=user, schema=schema)
        return dict(self.response or {})


_injected: Any = None


def set_llm(client: Any) -> None:
    """테스트/서버가 LLM 클라이언트를 주입한다. DISABLED 를 주면 LLM 을 쓰지 않는다."""
    global _injected
    _injected = client


def reset_llm() -> None:
    global _injected
    _injected = None


def api_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def get_llm(model: str | None = None) -> LlmClient | None:
    """설정에 따라 LLM 클라이언트를 돌려준다. 쓰지 않으면 None. model 은 기본 모델 대신 쓸 모델."""
    if _injected is DISABLED:
        return None
    if _injected is not None:
        return _injected
    backend = config.NL_BACKEND
    if backend == "rules":
        return None
    if backend == "auto" and not api_key_present():
        return None
    if backend == "llm" and not api_key_present():
        log.warning("FAIRMEET_NL_BACKEND=llm 이지만 ANTHROPIC_API_KEY 가 없어 규칙 기반을 씁니다")
        return None
    return AnthropicLlm(model=model)
