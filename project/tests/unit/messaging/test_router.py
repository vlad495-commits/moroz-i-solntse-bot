from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import json

import pytest

from moroz.messaging.router import (
    ROUTER_RESPONSE_FORMAT,
    LLMIntentRouter,
    RouteDecision,
    RouterVerdict,
    deterministic_route,
    route_message,
)
from moroz.security.llm_gateway import LLMResponse, LLMUnavailable, LLMUsage


class ScriptedProvider:
    def __init__(self, event):
        self.event = event
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if isinstance(self.event, BaseException):
            raise self.event
        return self.event


def router_response(text: str) -> LLMResponse:
    usage = LLMUsage("router", 11, 3, 2, 14, "router-model")
    return LLMResponse(text, 11, 3, 2, 14, "router-model", (usage,))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Сколько стоит криотерапия?", ("faq",)),
        ("Хочу записаться", ("booking",)),
        ("Перенесите мою запись", ("booking_change",)),
        ("Отмените мою запись", ("booking_cancel",)),
        ("Хочу пожаловаться", ("complaint",)),
        ("Позовите администратора", ("human_handoff",)),
        ("Спасибо!", ("smalltalk",)),
    ],
)
def test_deterministic_route_resolves_only_single_explicit_intent(
    text: str,
    expected: tuple[str, ...],
) -> None:
    assert deterministic_route(text) == RouteDecision(
        intents=expected,
        requires_clarification=False,
        source="deterministic",
        confidence=None,
        reason_code=None,
    )


@pytest.mark.parametrize(
    "text",
    [
        "Да, давайте завтра",
        "Нет, другую",
        "Сколько стоит и можно записаться?",
        "Перенесите или отмените запись",
        "А это как?",
    ],
)
def test_deterministic_route_returns_none_for_context_or_multi_intent(
    text: str,
) -> None:
    assert deterministic_route(text) is None
    assert route_message(text).intents == ("unknown",)


@pytest.mark.parametrize(
    "text",
    [
        "Мой телефон <PII_PHONE_1>, а сколько это?",
        "Мой телефон +7 900 111-22-33, а сколько это?",
        "Подскажите, мой телефон записан правильно?",
        "Мой телефон есть у вас?",
        "У вас есть мой телефон?",
    ],
)
def test_deterministic_route_does_not_guess_intent_from_contact_metadata(
    text: str,
) -> None:
    assert deterministic_route(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Какой у вас телефон?",
        "Какие у вас контакты?",
        "Подскажите контакты центра",
        "Ваш телефон?",
    ],
)
def test_deterministic_route_resolves_explicit_center_contact_questions(
    text: str,
) -> None:
    assert deterministic_route(text) == RouteDecision(("faq",), False)


@pytest.mark.asyncio
async def test_llm_router_accepts_strict_multi_intent_and_derives_conflict() -> None:
    provider = ScriptedProvider(
        router_response(
            json.dumps(
                {
                    "intents": ["booking_change", "booking_cancel"],
                    "confidence": 0.91,
                }
            )
        )
    )

    verdict = await LLMIntentRouter(provider).route(
        "Перенесите или отмените запись",
        [],
    )

    assert verdict.decision.intents == ("booking_change", "booking_cancel")
    assert verdict.decision.requires_clarification is True
    assert verdict.source == "llm"
    assert verdict.confidence == 0.91
    assert verdict.reason_code is None
    assert provider.requests[0].purpose == "router"
    assert provider.requests[0].response_format == ROUTER_RESPONSE_FORMAT


@pytest.mark.asyncio
async def test_router_context_is_last_six_roles_and_2000_chars() -> None:
    provider = ScriptedProvider(
        router_response('{"intents":["faq"],"confidence":0.8}')
    )
    context = [
        {"role": "user", "content": f"message-{index}-" + "x" * 500}
        for index in range(8)
    ] + [{"role": "system", "content": "must-not-leave"}]

    await LLMIntentRouter(provider).route("Сколько стоит?", context)

    prompt = provider.requests[0].messages[-1]["content"]
    assert "message-0" not in prompt
    assert "message-1" not in prompt
    assert "must-not-leave" not in prompt
    assert "UNTRUSTED_RECENT_CONTEXT" in prompt
    assert len(prompt) <= 2200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"intents":["faq"],"confidence":0.9}\n```',
        '{"intents":["faq"],"confidence":0.9,"extra":true}',
        '{"intents":["unknown-intent"],"confidence":0.9}',
        '{"intents":["faq","faq"],"confidence":0.9}',
        '{"intents":["faq","booking","other","unknown"],"confidence":0.9}',
        '{"intents":[],"confidence":0.9}',
        '{"intents":["faq"],"confidence":true}',
        '{"intents":["faq"],"confidence":NaN}',
        '{"intents":["faq"],"confidence":1.1}',
    ],
)
async def test_invalid_router_output_uses_unknown_without_false_confidence(
    raw: str,
) -> None:
    verdict = await LLMIntentRouter(
        ScriptedProvider(router_response(raw))
    ).route("Неоднозначный текст", [])

    assert verdict.decision.intents == ("unknown",)
    assert verdict.source == "fallback"
    assert verdict.confidence is None
    assert verdict.reason_code == "invalid_router_output"


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized_and_cancellation_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "provider-secret"
    failed = await LLMIntentRouter(
        ScriptedProvider(LLMUnavailable(secret))
    ).route("Неоднозначный текст", [])
    assert failed.decision.intents == ("unknown",)
    assert failed.reason_code == "router_unavailable"
    assert secret not in caplog.text

    cancellation = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as raised:
        await LLMIntentRouter(ScriptedProvider(cancellation)).route("текст", [])
    assert raised.value is cancellation


def test_router_verdict_is_immutable_and_does_not_echo_input() -> None:
    text = "неизвестный секретный пользовательский маркер"
    decision = route_message(text)
    verdict = RouterVerdict(decision)
    assert text not in repr(verdict)
    with pytest.raises(FrozenInstanceError):
        verdict.decision = decision  # type: ignore[misc]
