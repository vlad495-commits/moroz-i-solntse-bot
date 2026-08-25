from __future__ import annotations

import asyncio
import json

import pytest

from moroz.security.llm_gateway import LLMResponse, LLMUnavailable, LLMUsage
from moroz.security.output_validator import (
    OUTPUT_VALIDATOR_RESPONSE_FORMAT,
    OUTPUT_VALIDATOR_SYSTEM_PROMPT,
    LLMOutputValidator,
    OutputValidationDecision,
)


class Provider:
    def __init__(self, event):
        self.event = event
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if isinstance(self.event, BaseException):
            raise self.event
        return self.event


def test_system_prompt_defines_product_actions_and_category_precedence():
    prompt = OUTPUT_VALIDATOR_SYSTEM_PROMPT.lower()

    for required_rule in (
        "booking",
        "cancellation",
        "call",
        "gift",
        "without confirmed tool data",
        "choose product_rule",
        "choose incomplete for meaningless or gibberish text",
    ):
        assert required_rule in prompt


def response(payload):
    usage = LLMUsage("validator", 12, 4, 0, 16, "validator-model")
    return LLMResponse(
        json.dumps(payload),
        12,
        4,
        0,
        16,
        "validator-model",
        (usage,),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            {"action": "allow", "category": "safe"},
            OutputValidationDecision("allow", "llm", "safe"),
        ),
        *(
            (
                {"action": "regenerate", "category": category},
                OutputValidationDecision("regenerate", "llm", category),
            )
            for category in (
                "non_russian",
                "incomplete",
                "technical_artifact",
                "unprofessional",
                "product_rule",
                "unsafe_advice",
            )
        ),
    ],
)
async def test_strict_valid_verdict(payload, expected):
    provider = Provider(response(payload))

    verdict = await LLMOutputValidator(provider).validate(
        masked_input="Подскажите цену",
        masked_context=[{"role": "assistant", "content": "Уточните услугу"}],
        route_metadata="ROUTE intents=faq",
        candidate="Стоимость указана в прайсе.",
    )

    assert verdict.decision == expected
    assert verdict.usage[0].purpose == "validator"
    request = provider.requests[0]
    assert request.purpose == "validator"
    assert request.response_format == OUTPUT_VALIDATOR_RESPONSE_FORMAT
    assert request.messages[0]["role"] == "system"
    data = json.loads(request.messages[1]["content"])
    assert data == {
        "input": "Подскажите цену",
        "context": [{"role": "assistant", "content": "Уточните услугу"}],
        "route": "ROUTE intents=faq",
        "candidate": "Стоимость указана в прайсе.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "OK",
        "{}",
        '{"action":"allow","category":"non_russian"}',
        '{"action":"regenerate","category":"safe"}',
        '{"action":"regenerate","category":"unknown"}',
        '{"action":"regenerate","category":"incomplete","extra":1}',
    ],
)
async def test_invalid_output_allows_locally_safe_candidate_and_alerts(raw):
    alerts = []
    provider = Provider(LLMResponse(raw, 1, 1, 0, 2, "model"))

    verdict = await LLMOutputValidator(provider, alerts.append).validate(
        masked_input="masked",
        masked_context=[],
        route_metadata="ROUTE intents=faq",
        candidate="Безопасный ответ",
    )

    assert verdict.decision == OutputValidationDecision(
        "allow",
        "fallback",
        "validator_invalid_output",
    )
    assert alerts == ["validator_invalid_output"]


@pytest.mark.asyncio
async def test_unavailable_and_alert_failure_do_not_leak_payload(caplog):
    async def broken_alert(_code):
        raise RuntimeError("alert-secret")

    verdict = await LLMOutputValidator(
        Provider(LLMUnavailable("provider-secret")),
        broken_alert,
    ).validate(
        masked_input="private-input",
        masked_context=[],
        route_metadata="ROUTE intents=faq",
        candidate="private-candidate",
    )

    assert verdict.decision == OutputValidationDecision(
        "allow",
        "fallback",
        "validator_unavailable",
    )
    assert "provider-secret" not in caplog.text
    assert "private-input" not in caplog.text
    assert "private-candidate" not in caplog.text
    assert "alert-secret" not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await LLMOutputValidator(
            Provider(asyncio.CancelledError())
        ).validate(
            masked_input="masked",
            masked_context=[],
            route_metadata="ROUTE intents=faq",
            candidate="candidate",
        )
