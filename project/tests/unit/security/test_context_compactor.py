from __future__ import annotations

import asyncio
import json

import pytest

from moroz.security.context_compactor import (
    COMPACT_RESPONSE_FORMAT,
    ContextCompactor,
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


def dialog(count: int) -> list[dict[str, str]]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"сообщение {index}",
        }
        for index in range(count)
    ]


def payload(**changes) -> dict:
    data = {
        "version": 1,
        "facts": ["Гость интересуется криотерапией"],
        "agreements": ["Сначала уточнить противопоказания"],
        "open_questions": ["Какой день удобен"],
        "constraints": ["Удобно после 18:00"],
        "conflicts": ["Сначала утром ↔ затем после 18:00"],
    }
    data.update(changes)
    return data


def response(data: dict | str) -> LLMResponse:
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    usage = LLMUsage("compact", 20, 8, 0, 28, "compact-model")
    return LLMResponse(text, 20, 8, 0, 28, "compact-model", (usage,))


@pytest.mark.asyncio
async def test_exact_threshold_is_unchanged_without_provider_call():
    provider = Provider(AssertionError("provider must not run"))
    source = dialog(30)

    result = await ContextCompactor(provider).compact(source)

    assert result.source == "unchanged"
    assert result.reason_code == "below_threshold"
    assert result.messages == tuple(source)
    assert result.usage == ()
    assert provider.requests == []


@pytest.mark.asyncio
async def test_long_context_returns_strict_summary_and_exact_tail():
    provider = Provider(response(payload()))
    source = dialog(31)

    result = await ContextCompactor(provider).compact(source)

    assert result.source == "llm"
    assert result.reason_code == "compacted"
    assert result.messages[-10:] == tuple(source[-10:])
    assert result.messages[0] == {
        "role": "user",
        "content": (
            "UNTRUSTED_COMPACT_CONTEXT_V1\n"
            "Факты:\n- Гость интересуется криотерапией\n"
            "Договорённости:\n- Сначала уточнить противопоказания\n"
            "Открытые вопросы:\n- Какой день удобен\n"
            "Ограничения:\n- Удобно после 18:00\n"
            "Конфликты:\n- Сначала утром ↔ затем после 18:00"
        ),
    }
    assert result.usage[0].purpose == "compact"
    request = provider.requests[0]
    assert request.purpose == "compact"
    assert request.response_format == COMPACT_RESPONSE_FORMAT
    assert request.messages[0]["role"] == "system"
    request_data = json.loads(request.messages[1]["content"])
    assert request_data == {"history": source[:-10]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not-json",
        "```json\n{}\n```",
        json.dumps({"version": 1}),
        json.dumps({**payload(), "extra": []}),
        json.dumps(payload(version=2)),
        json.dumps(payload(facts=[True])),
        json.dumps(payload(facts=["x" * 301])),
        json.dumps(payload(facts=[str(index) for index in range(13)])),
        json.dumps(payload(facts=["+7 999 123-45-67"])),
        json.dumps(payload(facts=["Неизвестный <PII_PHONE_99>"])),
        json.dumps(
            payload(
                facts=[],
                agreements=[],
                open_questions=[],
                constraints=[],
                conflicts=[],
            )
        ),
    ],
)
async def test_invalid_output_returns_exact_tail_and_alerts(raw):
    alerts = []
    source = dialog(31)
    provider = Provider(response(raw))

    result = await ContextCompactor(provider, alerts.append).compact(source)

    assert result.source == "fallback"
    assert result.reason_code == "compact_invalid_output"
    assert result.messages == tuple(source[-10:])
    assert result.usage[0].purpose == "compact"
    assert alerts == ["compact_invalid_output"]


@pytest.mark.asyncio
async def test_existing_masked_placeholder_is_allowed_in_summary():
    provider = Provider(response(payload(facts=["Телефон <PII_PHONE_1>"])))
    source = dialog(31)
    source[0]["content"] = "Телефон <PII_PHONE_1>"

    result = await ContextCompactor(provider).compact(source)

    assert result.source == "llm"
    assert "<PII_PHONE_1>" in result.messages[0]["content"]


@pytest.mark.asyncio
async def test_provider_failure_and_alert_failure_are_safe(caplog):
    async def broken_alert(_code):
        raise RuntimeError("alert-secret")

    source = dialog(31)
    source[0]["content"] = "history-secret"
    result = await ContextCompactor(
        Provider(LLMUnavailable("provider-secret")),
        broken_alert,
    ).compact(source)

    assert result.source == "fallback"
    assert result.reason_code == "compact_unavailable"
    assert result.messages == tuple(source[-10:])
    assert result.usage == ()
    assert "history-secret" not in caplog.text
    assert "provider-secret" not in caplog.text
    assert "alert-secret" not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await ContextCompactor(Provider(asyncio.CancelledError())).compact(dialog(31))


@pytest.mark.asyncio
async def test_filters_invalid_messages_before_threshold_and_request():
    provider = Provider(response(payload()))
    source = dialog(31) + [
        {"role": "system", "content": "system-secret"},
        {"role": "tool", "content": "tool-secret"},
        {"role": "user", "content": ""},
        {"role": "assistant", "content": None},
    ]

    result = await ContextCompactor(provider).compact(source)

    request_text = provider.requests[0].messages[1]["content"]
    assert result.messages[-10:] == tuple(dialog(31)[-10:])
    assert "system-secret" not in request_text
    assert "tool-secret" not in request_text


@pytest.mark.asyncio
async def test_old_history_is_bounded_at_message_boundaries():
    provider = Provider(response(payload()))
    source = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"marker-{index}-" + ("я" * 4000),
        }
        for index in range(40)
    ]

    await ContextCompactor(provider).compact(source)

    data = json.loads(provider.requests[0].messages[1]["content"])
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 24_000
    assert "marker-29-" in encoded
    assert "marker-0-" not in encoded
