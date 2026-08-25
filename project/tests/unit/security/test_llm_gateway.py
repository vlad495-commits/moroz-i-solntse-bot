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
    LLMUsage,
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
        self.calls = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.event, BaseException):
            raise self.event
        return self.event


class AnthropicClient:
    def __init__(self, event):
        self.event = event
        self.calls = []
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.event, BaseException):
            raise self.event
        return self.event


def request(purpose="answer"):
    return LLMRequest(
        messages=({"role": "user", "content": "safe"},),
        purpose=purpose,
    )


def provider(client, kind, model="model", temperature=0.2, max_tokens=100):
    return SDKProvider(
        client,
        kind,
        model,
        temperature,
        max_tokens,
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


def openai_response(**overrides):
    values = {
        "choices": [
            SimpleNamespace(message=SimpleNamespace(content="answer"))
        ],
        "usage": SimpleNamespace(
            prompt_tokens=7,
            completion_tokens=4,
            total_tokens=11,
            prompt_tokens_details=SimpleNamespace(cached_tokens=3),
        ),
        "model": "openai-model",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def anthropic_response(**overrides):
    values = {
        "content": [SimpleNamespace(type="text", text="answer")],
        "usage": SimpleNamespace(
            input_tokens=9,
            output_tokens=6,
            cache_read_input_tokens=2,
        ),
        "model": "anthropic-model",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_provider_owns_generation_settings_and_request_retains_purpose(
    kind,
):
    raw = openai_response() if kind == "openai" else anthropic_response()
    client = OpenAIClient(raw) if kind == "openai" else AnthropicClient(raw)
    llm_request = request("guard")

    await provider(
        client,
        kind,
        temperature=0.7,
        max_tokens=321,
    ).complete(llm_request)

    assert llm_request.purpose == "guard"
    assert not hasattr(llm_request, "temperature")
    assert not hasattr(llm_request, "max_tokens")
    expected = {
        "model": "model",
        "messages": [{"role": "user", "content": "safe"}],
        "temperature": 0.7,
        "max_tokens": 321,
    }
    if kind == "anthropic":
        expected["system"] = ""
    assert client.calls == [expected]


@pytest.mark.asyncio
async def test_openai_receives_response_format_and_usage_keeps_purpose():
    schema = {
        "type": "json_schema",
        "json_schema": {"name": "route", "schema": {}},
    }
    client = OpenAIClient(openai_response())

    result = await provider(client, "openai").complete(
        LLMRequest(
            messages=({"role": "user", "content": "safe"},),
            purpose="router",
            response_format=schema,
        )
    )

    assert client.calls[0]["response_format"] == schema
    assert result.usage == (
        LLMUsage("router", 7, 4, 3, 11, "openai-model"),
    )


@pytest.mark.asyncio
async def test_anthropic_ignores_provider_schema_but_keeps_local_contract():
    client = AnthropicClient(anthropic_response())
    result = await provider(client, "anthropic").complete(
        LLMRequest(
            messages=({"role": "user", "content": "safe"},),
            purpose="router",
            response_format={"type": "json_schema"},
        )
    )

    assert "response_format" not in client.calls[0]
    assert result.usage[0].purpose == "router"


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
@pytest.mark.parametrize("status", [408, 409, 429, 500, 503, 599])
async def test_retryable_sdk_statuses_are_sanitized(kind, status):
    client = (
        OpenAIClient(_status_error(kind, status))
        if kind == "openai"
        else AnthropicClient(_status_error(kind, status))
    )

    with pytest.raises(RetryableLLMError) as raised:
        await provider(client, kind).complete(request())

    evidence = str(raised.value) + repr(raised.value)
    assert evidence == (
        "retryable_llm_error"
        "RetryableLLMError('retryable_llm_error')"
    )
    assert "sentinel" not in evidence
    assert "provider.invalid" not in evidence
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [400, 401, 403, 422, 600])
async def test_non_retryable_sdk_statuses_are_sanitized(kind, status):
    client = (
        OpenAIClient(_status_error(kind, status))
        if kind == "openai"
        else AnthropicClient(_status_error(kind, status))
    )

    with pytest.raises(NonRetryableLLMError) as raised:
        await provider(client, kind).complete(request())

    evidence = str(raised.value) + repr(raised.value)
    assert evidence == (
        "non_retryable_llm_error"
        "NonRetryableLLMError('non_retryable_llm_error')"
    )
    assert "sentinel" not in evidence
    assert "provider.invalid" not in evidence
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


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

    with pytest.raises(RetryableLLMError) as raised:
        await provider(client, kind).complete(request())

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "anthropic"])
async def test_unexpected_client_exception_propagates_unchanged(kind):
    unexpected = ValueError("programming-error")
    client = (
        OpenAIClient(unexpected)
        if kind == "openai"
        else AnthropicClient(unexpected)
    )

    with pytest.raises(ValueError) as raised:
        await provider(client, kind).complete(request())

    assert raised.value is unexpected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,raw",
    [
        (
            "openai",
            openai_response(choices=[]),
        ),
        (
            "openai",
            openai_response(
                choices=[SimpleNamespace(message=SimpleNamespace())]
            ),
        ),
        (
            "openai",
            openai_response(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="")
                    )
                ]
            ),
        ),
        (
            "openai",
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="answer")
                    )
                ],
                model="model",
            ),
        ),
        (
            "openai",
            openai_response(
                usage=SimpleNamespace(
                    prompt_tokens="7",
                    completion_tokens=4,
                    total_tokens=11,
                )
            ),
        ),
        (
            "anthropic",
            anthropic_response(content=[]),
        ),
        (
            "anthropic",
            anthropic_response(
                content=[SimpleNamespace(type="tool_use")]
            ),
        ),
        (
            "anthropic",
            anthropic_response(
                content=[SimpleNamespace(type="text", text="")]
            ),
        ),
        (
            "anthropic",
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="answer")],
                model="model",
            ),
        ),
        (
            "anthropic",
            anthropic_response(
                usage=SimpleNamespace(
                    input_tokens=9,
                    output_tokens=-1,
                )
            ),
        ),
    ],
    ids=[
        "openai-empty-choices",
        "openai-missing-content",
        "openai-empty-content",
        "openai-missing-usage",
        "openai-invalid-usage",
        "anthropic-empty-content",
        "anthropic-no-text-content",
        "anthropic-empty-text",
        "anthropic-missing-usage",
        "anthropic-invalid-usage",
    ],
)
async def test_malformed_response_is_sanitized_without_reserve(kind, raw):
    raw.secret = "raw-response-sentinel"
    client = OpenAIClient(raw) if kind == "openai" else AnthropicClient(raw)
    reserve = ScriptedProvider(response("must-not-run"))
    gateway = PrimaryReserveGateway(provider(client, kind), reserve)

    with pytest.raises(NonRetryableLLMError) as raised:
        await gateway.complete(request())

    evidence = str(raised.value) + repr(raised.value)
    assert evidence == (
        "non_retryable_llm_error"
        "NonRetryableLLMError('non_retryable_llm_error')"
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert "sentinel" not in evidence
    assert reserve.calls == 0


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

    adapted = await provider(
        OpenAIClient(raw), "openai", "configured"
    ).complete(request())

    assert adapted == LLMResponse(
        text="answer",
        prompt_tokens=7,
        completion_tokens=4,
        cached_tokens=3,
        total_tokens=11,
        model="openai-model",
        usage=(LLMUsage("answer", 7, 4, 3, 11, "openai-model"),),
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

    adapted = await provider(
        AnthropicClient(raw), "anthropic", "configured"
    ).complete(request())

    assert adapted == LLMResponse(
        text="first\nsecond",
        prompt_tokens=9,
        completion_tokens=6,
        cached_tokens=2,
        total_tokens=15,
        model="anthropic-model",
        usage=(LLMUsage("answer", 9, 6, 2, 15, "anthropic-model"),),
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
