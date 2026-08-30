import asyncio

import pytest

from moroz.security.input_security import (
    INPUT_SECURITY_SYSTEM_PROMPT,
    InputSecurityDecision,
    LLMInputSecurityClassifier,
)
from moroz.security.llm_gateway import LLMResponse, LLMUsage


class Provider:
    def __init__(self, *events):
        self.events = list(events)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


def response(text: str, model: str = "security-model") -> LLMResponse:
    usage = LLMUsage("security", 10, 1, 0, 11, model)
    return LLMResponse(text, 10, 1, 0, 11, model, (usage,))


def test_prompt_allows_correction_of_a_service_choice():
    prompt = INPUT_SECURITY_SYSTEM_PROMPT.casefold()

    assert "исправить выбор услуги" in prompt
    assert "не означает смену системных правил" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("OK", InputSecurityDecision("allow", "llm", "ok")),
        (" block\n", InputSecurityDecision("block", "llm", "block")),
    ],
)
async def test_accepts_only_ok_or_block_words(raw, expected):
    primary = Provider(response(raw))

    verdict = await LLMInputSecurityClassifier(primary).classify("masked-current")

    assert verdict.decision == expected
    assert verdict.usage[0].purpose == "security"
    request = primary.requests[0]
    assert request.purpose == "security"
    assert request.response_format is None
    assert request.messages[1] == {"role": "user", "content": "masked-current"}
    assert "CONTEXT" not in request.messages[1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_event", [RuntimeError("down"), response("MAYBE")])
async def test_primary_error_or_invalid_output_uses_reserve(primary_event):
    primary = Provider(primary_event)
    reserve = Provider(response("OK", "reserve-model"))

    verdict = await LLMInputSecurityClassifier(primary, reserve).classify("masked")

    assert verdict.decision == InputSecurityDecision("allow", "llm", "ok")
    assert len(primary.requests) == 1
    assert len(reserve.requests) == 1
    assert verdict.usage[-1].model == "reserve-model"


@pytest.mark.asyncio
async def test_both_models_down_fail_open_and_alert_without_private_data(caplog):
    alerts = []
    primary = Provider(RuntimeError("primary-secret"))
    reserve = Provider(RuntimeError("reserve-secret"))

    verdict = await LLMInputSecurityClassifier(
        primary,
        reserve,
        alerts.append,
    ).classify("private-input-sentinel")

    assert verdict.decision == InputSecurityDecision(
        "allow",
        "fallback",
        "security_down",
    )
    assert alerts == ["security_down"]
    assert "private-input-sentinel" not in caplog.text
    assert "primary-secret" not in caplog.text
    assert "reserve-secret" not in caplog.text


@pytest.mark.asyncio
async def test_invalid_outputs_from_both_models_fail_open_and_alert():
    alerts = []
    classifier = LLMInputSecurityClassifier(
        Provider(response("ALLOW")),
        Provider(response("BLOCK because")),
        alerts.append,
    )

    verdict = await classifier.classify("masked")

    assert verdict.decision.reason_code == "security_down"
    assert verdict.decision.action == "allow"
    assert alerts == ["security_down"]
    assert [item.model for item in verdict.usage] == [
        "security-model",
        "security-model",
    ]


@pytest.mark.asyncio
async def test_alert_failure_still_fails_open_and_logs_only_error_type(caplog):
    async def broken_alert(_code):
        raise RuntimeError("alert-secret")

    verdict = await LLMInputSecurityClassifier(
        Provider(RuntimeError("primary-secret")),
        Provider(RuntimeError("reserve-secret")),
        broken_alert,
    ).classify("private-input")

    assert verdict.decision.action == "allow"
    assert "alert-secret" not in caplog.text
    assert "private-input" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


@pytest.mark.asyncio
async def test_cancellation_propagates_without_calling_reserve():
    reserve = Provider(response("OK"))

    with pytest.raises(asyncio.CancelledError):
        await LLMInputSecurityClassifier(
            Provider(asyncio.CancelledError()),
            reserve,
        ).classify("masked")

    assert reserve.requests == []
