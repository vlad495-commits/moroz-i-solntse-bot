from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

import anthropic
import openai
import llm as legacy_llm
from moroz.security.llm_gateway import (
    LLMRequest,
    LLMResponse,
    LLMUnavailable,
    NonRetryableLLMError,
    PrimaryReserveGateway,
    RetryableLLMError,
    SDKProvider,
)


class ScriptedProvider:
    def __init__(self, *events):
        self.events = list(events)
        self.calls = 0

    async def complete(self, _request):
        self.calls += 1
        event = self.events.pop(0)
        if isinstance(event, BaseException):
            raise event
        return event


class OpenAIClient:
    def __init__(self, event):
        self.event = event
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    async def create(self, **_kwargs):
        if isinstance(self.event, BaseException):
            raise self.event
        return self.event


class AnthropicClient:
    def __init__(self, event):
        self.event = event
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **_kwargs):
        if isinstance(self.event, BaseException):
            raise self.event
        return self.event


def request():
    return LLMRequest(
        messages=({"role": "user", "content": "safe"},),
        temperature=0.2,
        max_tokens=100,
    )


def response(text="primary"):
    return LLMResponse(
        text=text,
        prompt_tokens=3,
        completion_tokens=2,
        cached_tokens=1,
        total_tokens=5,
        model="model",
    )


@pytest.mark.asyncio
async def test_primary_success_never_calls_reserve():
    primary = ScriptedProvider(response())
    reserve = ScriptedProvider(response("must-not-run"))

    result = await PrimaryReserveGateway(primary, reserve).complete(request())

    assert result.text == "primary"
    assert primary.calls == 1
    assert reserve.calls == 0


@pytest.mark.asyncio
async def test_retryable_primary_failure_calls_reserve_once():
    primary = ScriptedProvider(RetryableLLMError())
    reserve = ScriptedProvider(response("reserve"))

    result = await PrimaryReserveGateway(primary, reserve).complete(request())

    assert result.text == "reserve"
    assert primary.calls == 1
    assert reserve.calls == 1


@pytest.mark.asyncio
async def test_non_retryable_primary_failure_does_not_call_reserve():
    primary = ScriptedProvider(NonRetryableLLMError())
    reserve = ScriptedProvider(response("must-not-run"))

    with pytest.raises(NonRetryableLLMError):
        await PrimaryReserveGateway(primary, reserve).complete(request())

    assert primary.calls == 1
    assert reserve.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reserve", [None, ScriptedProvider(RetryableLLMError())])
async def test_unavailable_paths_are_sanitized_and_bounded(reserve):
    primary = ScriptedProvider(RetryableLLMError())

    with pytest.raises(LLMUnavailable) as raised:
        await PrimaryReserveGateway(primary, reserve).complete(request())

    assert str(raised.value) == "llm_unavailable"
    assert repr(raised.value) == "LLMUnavailable('llm_unavailable')"
    assert primary.calls == 1
    if reserve is not None:
        assert reserve.calls == 1


@pytest.mark.asyncio
async def test_unexpected_python_exception_propagates_unchanged():
    unexpected = ValueError("programming-error")
    primary = ScriptedProvider(unexpected)

    with pytest.raises(ValueError) as raised:
        await PrimaryReserveGateway(primary).complete(request())

    assert raised.value is unexpected


def _status_error(kind, status):
    request = httpx.Request(
        "POST",
        "https://user:password-sentinel@provider.invalid/v1",
        headers={"Authorization": "Bearer token-sentinel"},
    )
    response = httpx.Response(status, request=request)
    error_type = (
        openai.APIStatusError
        if kind == "openai"
        else anthropic.APIStatusError
    )
    return error_type(
        "raw-provider-sentinel",
        response=response,
        body={"detail": "raw-response-sentinel"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [408, 409, 429, 500, 503])
async def test_retryable_sdk_statuses_are_sanitized(kind, status):
    client = (
        OpenAIClient(_status_error(kind, status))
        if kind == "openai"
        else AnthropicClient(_status_error(kind, status))
    )

    with pytest.raises(RetryableLLMError) as raised:
        await SDKProvider(client, kind, "model").complete(request())

    evidence = str(raised.value) + repr(raised.value)
    assert evidence == (
        "retryable_llm_error"
        "RetryableLLMError('retryable_llm_error')"
    )
    assert "sentinel" not in evidence
    assert "provider.invalid" not in evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_non_retryable_sdk_statuses_are_sanitized(kind, status):
    client = (
        OpenAIClient(_status_error(kind, status))
        if kind == "openai"
        else AnthropicClient(_status_error(kind, status))
    )

    with pytest.raises(NonRetryableLLMError) as raised:
        await SDKProvider(client, kind, "model").complete(request())

    evidence = str(raised.value) + repr(raised.value)
    assert evidence == (
        "non_retryable_llm_error"
        "NonRetryableLLMError('non_retryable_llm_error')"
    )
    assert "sentinel" not in evidence
    assert "provider.invalid" not in evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("error_name", ["connection", "timeout"])
async def test_sdk_connection_and_timeout_are_retryable(kind, error_name):
    raw_request = httpx.Request("POST", "https://provider.invalid/v1")
    module = openai if kind == "openai" else anthropic
    error_type = (
        module.APIConnectionError
        if error_name == "connection"
        else module.APITimeoutError
    )
    error = error_type(request=raw_request)
    client = (
        OpenAIClient(error)
        if kind == "openai"
        else AnthropicClient(error)
    )

    with pytest.raises(RetryableLLMError):
        await SDKProvider(client, kind, "model").complete(request())


@pytest.mark.asyncio
async def test_openai_response_is_adapted_without_raw_response_storage():
    raw = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="answer"))
        ],
        usage=SimpleNamespace(
            prompt_tokens=7,
            completion_tokens=4,
            total_tokens=11,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        ),
        model="openai-model",
        secret="raw-response-sentinel",
    )

    adapted = await SDKProvider(
        OpenAIClient(raw), "openai", "configured"
    ).complete(request())

    assert adapted == LLMResponse(
        text="answer",
        prompt_tokens=7,
        completion_tokens=4,
        cached_tokens=3,
        total_tokens=11,
        model="openai-model",
    )
    assert "raw-response-sentinel" not in repr(adapted)


@pytest.mark.asyncio
async def test_anthropic_response_is_adapted_without_raw_response_storage():
    raw = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="first"),
            SimpleNamespace(type="tool_use", text="ignored"),
            SimpleNamespace(type="text", text="second"),
        ],
        usage=SimpleNamespace(
            input_tokens=9,
            output_tokens=6,
            cache_read_input_tokens=2,
        ),
        model="anthropic-model",
        secret="raw-response-sentinel",
    )

    adapted = await SDKProvider(
        AnthropicClient(raw), "anthropic", "configured"
    ).complete(request())

    assert adapted == LLMResponse(
        text="first\nsecond",
        prompt_tokens=9,
        completion_tokens=6,
        cached_tokens=2,
        total_tokens=15,
        model="anthropic-model",
    )
    assert "raw-response-sentinel" not in repr(adapted)


def test_with_text_preserves_usage_and_optionally_changes_model():
    original = response()

    same_model = original.with_text("validated")
    changed_model = original.with_text(
        "fallback", model="security-fallback"
    )

    assert same_model == LLMResponse(
        text="validated",
        prompt_tokens=3,
        completion_tokens=2,
        cached_tokens=1,
        total_tokens=5,
        model="model",
    )
    assert changed_model.model == "security-fallback"
    assert changed_model.total_tokens == 5


def test_legacy_client_factory_disables_sdk_internal_retries(monkeypatch):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(legacy_llm, "AsyncOpenAI", Client)
    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)

    legacy_llm._create_client("configured", "https://custom.invalid", "openai")
    legacy_llm._create_client("configured", None, "anthropic")

    assert [call["max_retries"] for call in calls] == [0, 0]


def test_reserve_environment_is_limited_to_llm_runtime_services():
    services = yaml.safe_load(
        Path("/workspace/docker-compose.yml").read_text(encoding="utf-8")
    )["services"]
    reserve_keys = {"RESERVE_API_KEY", "RESERVE_BASE_URL", "RESERVE_MODEL"}

    for name in ("bot", "worker", "admin"):
        assert reserve_keys <= set(services[name]["environment"])
    for name in ("test", "migrate", "cutover", "scheduler"):
        assert reserve_keys.isdisjoint(
            services[name].get("environment", {})
        )
