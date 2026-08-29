from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import json

import pytest

import moroz.messaging.router as router_module
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


def test_routes_are_the_minimal_single_route_allowlist() -> None:
    assert router_module.ROUTES == (
        "consultation",
        "booking",
        "booking_management",
        "escalation",
        "smalltalk",
        "offtopic",
        "other",
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Сколько стоит криотерапия?", "consultation"),
        ("Какой у вас телефон?", "consultation"),
        ("Хочу записаться", "booking"),
        ("Перенесите мою запись", "booking_management"),
        ("Отмените мою запись", "booking_management"),
        ("Перенесите или отмените запись", "booking_management"),
        ("Хочу пожаловаться", "escalation"),
        ("Позовите администратора", "escalation"),
        ("Верните деньги за услугу", "escalation"),
        ("Спасибо!", "smalltalk"),
    ],
)
def test_deterministic_route_resolves_only_unambiguous_cases(
    text: str,
    expected: str,
) -> None:
    assert deterministic_route(text) == RouteDecision(expected, 1.0)


@pytest.mark.parametrize(
    "text",
    [
        "Да, давайте завтра",
        "Нет, другую",
        "Сколько стоит и можно записаться?",
        "А это как?",
        "Какой сегодня курс доллара?",
        "У вас есть вакансии?",
    ],
)
def test_deterministic_route_defers_context_mixed_and_nonlocal_cases(
    text: str,
) -> None:
    assert deterministic_route(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Я недоволен ценой и хочу записаться",
        "Отмените запись и позовите руководителя",
        "Спасибо, но хочу пожаловаться",
    ],
)
def test_explicit_escalation_has_safe_local_priority(text: str) -> None:
    assert deterministic_route(text) == RouteDecision("escalation", 1.0)


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
def test_deterministic_route_does_not_guess_from_contact_metadata(
    text: str,
) -> None:
    assert deterministic_route(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Спасибо", "smalltalk"),
        ("Спасибо!", "smalltalk"),
        ("Большое спасибо", "smalltalk"),
        ("Спасибо, хочу записаться", "booking"),
        ("Спасибо, но всё плохо", None),
    ],
)
def test_smalltalk_rule_is_anchored_to_the_whole_message(
    text: str,
    expected: str | None,
) -> None:
    decision = deterministic_route(text)
    assert (decision.route if decision else None) == expected


def test_route_message_uses_safe_general_fallback() -> None:
    assert route_message("Неоднозначный текст") == RouteDecision(
        "consultation",
        0.0,
    )


@pytest.mark.asyncio
async def test_llm_router_accepts_one_strict_route() -> None:
    provider = ScriptedProvider(
        router_response(
            json.dumps({"route": "booking", "confidence": 0.91})
        )
    )
    verdict = await LLMIntentRouter(provider).route(
        "Да, давайте завтра",
        [{"role": "assistant", "content": "Хотите записаться?"}],
    )
    assert verdict.decision == RouteDecision("booking", 0.91)
    assert verdict.source == "llm"
    assert verdict.reason_code is None
    assert verdict.confidence == 0.91
    assert provider.requests[0].purpose == "router"
    assert provider.requests[0].response_format == ROUTER_RESPONSE_FORMAT


@pytest.mark.asyncio
async def test_router_context_is_last_six_roles_and_2000_chars() -> None:
    provider = ScriptedProvider(
        router_response('{"route":"consultation","confidence":0.8}')
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
        '```json\n{"route":"consultation","confidence":0.9}\n```',
        'result: {"route":"consultation","confidence":0.9}',
        '{"route":"consultation","confidence":0.9,"extra":true}',
        '{"route":"unknown","confidence":0.9}',
        '{"intents":["consultation"],"confidence":0.9}',
        '{"route":["consultation"],"confidence":0.9}',
        '{"route":"consultation","confidence":true}',
        '{"route":"consultation","confidence":NaN}',
        '{"route":"consultation","confidence":1.1}',
        '{"route":"consultation","confidence":-0.1}',
    ],
)
async def test_invalid_router_output_uses_safe_general_route(raw: str) -> None:
    verdict = await LLMIntentRouter(
        ScriptedProvider(router_response(raw))
    ).route("Неоднозначный текст", [])
    assert verdict.decision == RouteDecision("consultation", 0.0)
    assert verdict.source == "fallback"
    assert verdict.confidence == 0.0
    assert verdict.reason_code == "invalid_router_output"


@pytest.mark.asyncio
async def test_provider_failure_is_sanitized_and_cancellation_propagates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "provider-secret"
    failed = await LLMIntentRouter(
        ScriptedProvider(LLMUnavailable(secret))
    ).route("Неоднозначный текст", [])
    assert failed.decision == RouteDecision("consultation", 0.0)
    assert failed.source == "fallback"
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
