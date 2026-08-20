from __future__ import annotations

from collections.abc import Mapping

from moroz.messaging.ingress import decide_ingress
from moroz.security.llm_gateway import (
    LLMRequest,
    LLMResponse,
    NonRetryableLLMError,
    PrimaryReserveGateway,
    RetryableLLMError,
)
from moroz.security.pipeline import SAFE_OUTPUT_FALLBACK, SecurityPipeline
from moroz.security.validator import extract_structured_facts


class _ScriptedProvider:
    def __init__(self, *events: LLMResponse | BaseException) -> None:
        self._events = events
        self.calls = 0

    async def complete(self, _request: LLMRequest) -> LLMResponse:
        event = self._events[self.calls]
        self.calls += 1
        if isinstance(event, BaseException):
            raise event
        return event


def _local_response(text: str, model: str) -> LLMResponse:
    return LLMResponse(text, 0, 0, 0, 0, model)


async def evaluate_structural_case(
    case: Mapping[str, object],
) -> bool | None:
    """Проверить локальные policy-кейсы без обращения к внешней LLM."""
    category = str(case.get("category") or "")
    if category == "consent":
        decision = decide_ingress(
            has_text=True,
            has_processing_consent=False,
        )
        return decision.action == "reply" and decision.code == "consent_required"
    if category == "nontext_voice":
        decision = decide_ingress(
            has_text=False,
            has_processing_consent=False,
        )
        return decision.action == "reply" and decision.code == "nontext"
    if category not in {
        "primary_reserve",
        "providers_unavailable",
        "nonretryable_provider",
    }:
        return None

    reserve_reply = "Ответ резервной модели"
    if category == "nonretryable_provider":
        primary = _ScriptedProvider(NonRetryableLLMError())
        reserve = _ScriptedProvider(_local_response("unexpected", "reserve"))
        expected_calls = (1, 0)
        expected_text = SAFE_OUTPUT_FALLBACK
    elif category == "providers_unavailable":
        primary = _ScriptedProvider(RetryableLLMError())
        reserve = _ScriptedProvider(RetryableLLMError())
        expected_calls = (1, 1)
        expected_text = SAFE_OUTPUT_FALLBACK
    else:
        primary = _ScriptedProvider(RetryableLLMError())
        reserve = _ScriptedProvider(_local_response(reserve_reply, "reserve"))
        expected_calls = (1, 1)
        expected_text = reserve_reply

    result = await SecurityPipeline(
        PrimaryReserveGateway(primary, reserve),
        "",
        extract_structured_facts(""),
    ).respond(
        str(case.get("input") or case.get("question") or "Безопасный вопрос"),
        [],
        recent_message_count=1,
    )
    return (
        (primary.calls, reserve.calls) == expected_calls
        and result.text == expected_text
    )
