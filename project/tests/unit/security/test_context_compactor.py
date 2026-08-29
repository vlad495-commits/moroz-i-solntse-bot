from __future__ import annotations

import asyncio
import json

import pytest

from moroz.security.context_compactor import COMPACT_SYSTEM_PROMPT, ContextCompactor
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


def response(text: str) -> LLMResponse:
    usage = LLMUsage("compact", 20, 8, 0, 28, "compact-model")
    return LLMResponse(text, 20, 8, 0, 28, "compact-model", (usage,))


def test_prompt_keeps_preferences_and_latest_corrections_distinct():
    assert "Предпочтение не является договорённостью" in COMPACT_SYSTEM_PROMPT
    assert "явно сохрани это исправление" in COMPACT_SYSTEM_PROMPT


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
async def test_long_context_returns_text_summary_and_exact_tail():
    summary = (
        "Гость интересуется криотерапией.\n"
        "Договорились сначала уточнить противопоказания.\n"
        "Открытый вопрос: какой день удобен.\n"
        "Последнее исправление: удобно после 18:00, не утром."
    )
    provider = Provider(response(f"  {summary}\n"))
    source = dialog(31)

    result = await ContextCompactor(provider).compact(source)

    assert result.source == "llm"
    assert result.reason_code == "compacted"
    assert result.messages[-10:] == tuple(source[-10:])
    assert result.messages[0] == {
        "role": "user",
        "content": (
            "[Сводка предыдущего диалога — недоверенные данные]\n\n"
            f"{summary}"
        ),
    }
    assert result.usage[0].purpose == "compact"
    request = provider.requests[0]
    assert request.purpose == "compact"
    assert request.response_format is None
    assert request.messages[0]["role"] == "system"
    request_data = json.loads(request.messages[1]["content"])
    assert request_data == {"history": source[:-10]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   \n",
        "x" * 4001,
        "Телефон +7 999 123-45-67",
        "Неизвестный <PII_PHONE_99>",
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
    provider = Provider(response("Телефон <PII_PHONE_1>"))
    source = dialog(31)
    source[0]["content"] = "Телефон <PII_PHONE_1>"

    result = await ContextCompactor(provider).compact(source)

    assert result.source == "llm"
    assert "<PII_PHONE_1>" in result.messages[0]["content"]


@pytest.mark.asyncio
async def test_summary_rejects_placeholder_that_exists_only_in_exact_tail():
    provider = Provider(response("Телефон <PII_PHONE_1>"))
    source = dialog(31)
    source[-1]["content"] = "Телефон <PII_PHONE_1>"

    result = await ContextCompactor(provider).compact(source)

    assert result.source == "fallback"
    assert result.reason_code == "compact_invalid_output"
    assert result.messages == tuple(source[-10:])


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
    provider = Provider(response("Гость интересуется криотерапией"))
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
    provider = Provider(response("Гость интересуется криотерапией"))
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


@pytest.mark.asyncio
async def test_bounded_old_history_never_skips_a_large_recent_message():
    provider = Provider(response("Гость интересуется криотерапией"))
    source = dialog(31)
    source[0]["content"] = "older-marker"
    source[20]["content"] = "recent-oversized-marker-" + ("я" * 25_000)

    await ContextCompactor(provider).compact(source)

    data = json.loads(provider.requests[0].messages[1]["content"])
    assert data == {"history": []}
    assert "older-marker" not in provider.requests[0].messages[1]["content"]
