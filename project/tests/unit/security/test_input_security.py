import asyncio
import json

import pytest

from moroz.security.input_security import (
    INPUT_SECURITY_RESPONSE_FORMAT,
    InputSecurityDecision,
    LLMInputSecurityClassifier,
)
from moroz.security.llm_gateway import LLMResponse, LLMUnavailable, LLMUsage


class Provider:
    def __init__(self, event):
        self.event = event
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if isinstance(self.event, BaseException):
            raise self.event
        return self.event


def response(payload):
    usage = LLMUsage("security", 10, 3, 0, 13, "security-model")
    return LLMResponse(
        json.dumps(payload),
        10,
        3,
        0,
        13,
        "security-model",
        (usage,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            {"action": "allow", "category": "safe"},
            InputSecurityDecision("allow", "llm", "safe"),
        ),
        (
            {"action": "block", "category": "prompt_attack"},
            InputSecurityDecision("block", "llm", "prompt_attack"),
        ),
        (
            {"action": "block", "category": "secret_request"},
            InputSecurityDecision("block", "llm", "secret_request"),
        ),
        (
            {"action": "block", "category": "third_party_pii"},
            InputSecurityDecision("block", "llm", "third_party_pii"),
        ),
        (
            {"action": "block", "category": "dangerous_content"},
            InputSecurityDecision("block", "llm", "dangerous_content"),
        ),
    ],
)
async def test_strict_valid_verdict(payload, expected):
    provider = Provider(response(payload))

    verdict = await LLMInputSecurityClassifier(provider).classify("masked", [])

    assert verdict.decision == expected
    assert verdict.usage[0].purpose == "security"
    assert provider.requests[0].purpose == "security"
    assert provider.requests[0].response_format == INPUT_SECURITY_RESPONSE_FORMAT


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "ALLOW",
        "{}",
        '{"action":"allow","category":"prompt_attack"}',
        '{"action":"block","category":"safe"}',
        '{"action":"block","category":"unknown"}',
        '{"action":"block","category":"prompt_attack","extra":1}',
    ],
)
async def test_invalid_output_fails_closed_and_alerts(raw):
    alerts = []
    provider = Provider(LLMResponse(raw, 1, 1, 0, 2, "model"))

    verdict = await LLMInputSecurityClassifier(
        provider,
        alerts.append,
    ).classify("masked", [])

    assert verdict.decision == InputSecurityDecision(
        "block",
        "fallback",
        "security_invalid_output",
    )
    assert alerts == ["security_invalid_output"]


@pytest.mark.asyncio
async def test_unavailable_and_alert_failure_still_fail_closed(caplog):
    async def broken_alert(_code):
        raise RuntimeError("alert-secret")

    verdict = await LLMInputSecurityClassifier(
        Provider(LLMUnavailable("provider-secret")),
        broken_alert,
    ).classify("private-input", [])

    assert verdict.decision == InputSecurityDecision(
        "block",
        "fallback",
        "security_unavailable",
    )
    assert "provider-secret" not in caplog.text
    assert "private-input" not in caplog.text
    assert "alert-secret" not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await LLMInputSecurityClassifier(
            Provider(asyncio.CancelledError())
        ).classify("masked", [])
