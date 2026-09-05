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
        ("Мои записи", "booking_management"),
        ("📅 Записаться", "booking"),
        ("✨ Услуги и цены", "consultation"),
        ("📍 Адрес и режим", "consultation"),
        ("👩‍💼 Позвать администратора", "escalation"),
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
    assert deterministic_route(text) == (RouteDecision(expected, 1.0) if text in {"📅 Записаться", "✨ Услуги и цены", "📍 Адрес и режим", "👩‍💼 Позвать администратора"} else None)


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
    assert deterministic_route(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "Хочу поговорить с администратором",
        "Можно администратора?",
    ],
)
def test_explicit_handoff_phrases_route_to_escalation(text: str) -> None:
    assert deterministic_route(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Жалоб нет, хочу записаться", "booking"),
        ("У меня жалоб нет, хочу записаться", "booking"),
        ("Не хочу жаловаться, хочу записаться", "booking"),
        ("Не хочу пожаловаться, хочу записаться", "booking"),
        ("Жалоб нет", None),
    ],
)
def test_negated_complaint_does_not_create_false_escalation(
    text: str,
    expected: str | None,
) -> None:
    decision = deterministic_route(text)
    assert decision is None


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
    assert decision is None


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
async def test_router_current_and_context_share_one_2000_character_budget() -> None:
    provider = ScriptedProvider(
        router_response('{"route":"consultation","confidence":0.8}')
    )
    current = "current-prefix-" + "x" * 4000 + "-current-tail"
    context = [{"role": "user", "content": "y" * 2000}]

    await LLMIntentRouter(provider).route(current, context)

    prompt = provider.requests[0].messages[-1]["content"]
    assert len(prompt) <= 2000
    assert "current-prefix" not in prompt
    assert "current-tail" in prompt


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
        '{"route":"escalation","route":"consultation","confidence":0.9}',
    ],
)
async def test_invalid_router_output_uses_safe_general_route(raw: str) -> None:
    verdict = await LLMIntentRouter(
        ScriptedProvider(router_response(raw))
    ).route("Неоднозначный текст", [])
    assert verdict.decision == RouteDecision("other", 0.0)
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
    assert failed.decision == RouteDecision("other", 0.0)
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


@pytest.mark.asyncio
async def test_router_extracts_structured_service_and_date():
    payload = {'route': 'booking', 'confidence': .97, 'action': 'create',
               'service': 'массаж', 'date': '2026-09-07', 'choice': None}
    provider = ScriptedProvider(router_response(json.dumps(payload)))
    verdict = await LLMIntentRouter(provider).route('Массаж 7 сентября', [])
    assert verdict.decision == RouteDecision('booking', .97, 'create', 'массаж', '2026-09-07')
    schema = ROUTER_RESPONSE_FORMAT['json_schema']['schema']
    assert set(schema['required']) == set(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('date', '2026-02-30'), ('choice', True), ('choice', -1), ('action', 'confirm'), ('service', 12)])
async def test_bad_booking_parameters_never_dispatch(field, value):
    payload = {'route': 'booking', 'confidence': .97, 'action': 'create',
               'service': 'массаж', 'date': None, 'choice': None}
    payload[field] = value
    verdict = await LLMIntentRouter(ScriptedProvider(router_response(json.dumps(payload)))).route('запись', [])
    assert verdict.source == 'fallback'


@pytest.mark.asyncio
async def test_state_has_its_own_budget_and_intact_untrusted_json():
    provider = ScriptedProvider(router_response('{"route":"consultation","confidence":0.9}'))
    state = json.dumps({'mode': 'catalog_browse', 'active': False, 'service': 'Массаж',
        'choices': [{'index': i, 'label': 'Расширенное название ' * 30} for i in range(76)]}, ensure_ascii=False)
    await LLMIntentRouter(provider).route('Массаж', [
        {'role': 'user', 'content': 'Сколько стоит массаж?'},
        {'role': 'assistant', 'content': 'Какой вид массажа вас интересует?'}], state=state)
    messages = provider.requests[0].messages
    assert len(messages) == 3
    assert messages[-1]['role'] == 'user'
    assert 'Какой вид массажа' in messages[1]['content']
    assert 'Сколько стоит массаж?' in messages[1]['content']
    assert len(messages[1]['content']) <= 2000
    assert len(messages[2]['content']) <= 2000
    bounded = json.loads(messages[2]['content'].split('\n', 1)[1])
    assert bounded['mode'] == 'catalog_browse'
    assert 0 < len(bounded['choices']) <= 8
    assert bounded['choices'][0]['index'] == 0


@pytest.mark.asyncio
async def test_long_assistant_description_cannot_remove_recent_user_service():
    provider = ScriptedProvider(router_response('{"route":"consultation","confidence":0.9}'))
    await LLMIntentRouter(provider).route('Сколько стоит?', [
        {'role': 'user', 'content': 'Расскажи про криомассаж головы'},
        {'role': 'assistant', 'content': 'Описание процедуры. ' * 250}])
    assert 'криомассаж головы' in provider.requests[0].messages[1]['content']


@pytest.mark.asyncio
async def test_long_assistant_keeps_service_at_start_and_question_at_end():
    provider = ScriptedProvider(router_response('{"route":"consultation","confidence":0.9}'))
    await LLMIntentRouter(provider).route('Сколько стоит?', [
        {'role': 'user', 'content': 'Подробнее'},
        {'role': 'assistant', 'content': 'Криомассаж головы. ' + 'Описание. ' * 500 + 'Что ещё рассказать?'}])
    content = provider.requests[0].messages[1]['content']
    assert 'Криомассаж головы' in content
    assert 'Что ещё рассказать?' in content


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['bad JSON', '[]', '{"unknown": "' + 'x' * 50000 + '"}'], ids=['invalid-json', 'non-object', 'large-unknown'])
async def test_invalid_or_unknown_state_is_omitted(state):
    provider = ScriptedProvider(router_response('{"route":"consultation","confidence":0.9}'))
    await LLMIntentRouter(provider).route('Вопрос', [], state=state)
    assert len(provider.requests[0].messages) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('route,action,choice', [
    ('consultation', 'create', None), ('booking', 'price', None),
    ('booking_management', 'create', None), ('booking', 'create', 0),
    ('booking', 'none', 0), ('booking', 'provide_name', 0),
])
async def test_route_action_and_choice_must_agree(route, action, choice):
    payload = dict(route=route, action=action, choice=choice, confidence=.99)
    result = await LLMIntentRouter(ScriptedProvider(router_response(json.dumps(payload)))).route('текст', [])
    assert result.source == 'fallback'


@pytest.mark.asyncio
async def test_global_choice_index_beyond_first_hundred_is_valid():
    provider = ScriptedProvider(router_response('{"route":"booking","confidence":0.99,"action":"continue","choice":108}'))
    result = await LLMIntentRouter(provider).route('Первый вариант на этой странице', [],
        state='{"mode":"booking","active":true,"choices":[{"index":108,"label":"Массаж"}]}')
    assert result.source == 'llm'
    assert result.decision.choice == 108
