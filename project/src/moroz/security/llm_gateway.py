from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Protocol

import anthropic
import openai


_MISSING = object()


class _SafeLLMError(RuntimeError):
    code = "llm_error"

    def __init__(self, *_ignored: object) -> None:
        super().__init__(self.code)


class RetryableLLMError(_SafeLLMError):
    code = "retryable_llm_error"


class NonRetryableLLMError(_SafeLLMError):
    code = "non_retryable_llm_error"


class LLMUnavailable(_SafeLLMError):
    code = "llm_unavailable"


@dataclass(frozen=True, slots=True)
class LLMRequest:
    messages: tuple[dict[str, str], ...]
    purpose: str


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    total_tokens: int
    model: str

    def with_text(
        self,
        text: str,
        *,
        model: str | None = None,
    ) -> LLMResponse:
        return replace(
            self,
            text=text,
            model=self.model if model is None else model,
        )


class Provider(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


def _retryable_status(status: int) -> bool:
    return status in {408, 409, 429} or 500 <= status <= 599


def _count(source: object, name: str, *, optional: bool = False) -> int:
    value = getattr(source, name, _MISSING)
    if optional and (value is _MISSING or value is None):
        return 0
    if type(value) is not int or value < 0:
        raise NonRetryableLLMError
    return value


def _anthropic_messages(
    messages: tuple[dict[str, str], ...],
) -> tuple[str, list[dict[str, str]]]:
    system = ""
    converted: list[dict[str, str]] = []
    for message in messages:
        if message["role"] == "system":
            system = message["content"]
        else:
            converted.append(dict(message))
    return system, converted


def _openai_response(response: object, fallback_model: str) -> LLMResponse:
    choices = getattr(response, "choices", _MISSING)
    if not isinstance(choices, (list, tuple)) or not choices:
        raise NonRetryableLLMError
    message = getattr(choices[0], "message", _MISSING)
    text = getattr(message, "content", _MISSING)
    if not isinstance(text, str) or not text.strip():
        raise NonRetryableLLMError
    usage = getattr(response, "usage", _MISSING)
    prompt_tokens = _count(usage, "prompt_tokens")
    completion_tokens = _count(usage, "completion_tokens")
    total_tokens = _count(usage, "total_tokens")
    details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = (
        0 if details is None else _count(details, "cached_tokens", optional=True)
    )
    return LLMResponse(
        text=text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        model=getattr(response, "model", None) or fallback_model,
    )


def _anthropic_response(response: object, fallback_model: str) -> LLMResponse:
    content = getattr(response, "content", _MISSING)
    if not isinstance(content, (list, tuple)) or not content:
        raise NonRetryableLLMError
    text_blocks: list[str] = []
    for block in content:
        if getattr(block, "type", None) != "text":
            continue
        text = getattr(block, "text", _MISSING)
        if not isinstance(text, str) or not text.strip():
            raise NonRetryableLLMError
        text_blocks.append(text)
    if not text_blocks:
        raise NonRetryableLLMError
    usage = getattr(response, "usage", _MISSING)
    prompt_tokens = _count(usage, "input_tokens")
    completion_tokens = _count(usage, "output_tokens")
    return LLMResponse(
        text="\n".join(text_blocks),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=_count(
            usage,
            "cache_read_input_tokens",
            optional=True,
        ),
        total_tokens=prompt_tokens + completion_tokens,
        model=getattr(response, "model", None) or fallback_model,
    )


class SDKProvider:
    def __init__(
        self,
        client: object,
        kind: Literal["openai", "anthropic"],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> None:
        self.client = client
        self.kind = kind
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    async def complete(self, request: LLMRequest) -> LLMResponse:
        translated_error: type[_SafeLLMError] | None = None
        try:
            if self.kind == "anthropic":
                system, messages = _anthropic_messages(request.messages)
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=messages,
                    temperature=self.temperature,
                )
            else:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=list(request.messages),
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
        except (
            openai.APITimeoutError,
            openai.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
        ):
            translated_error = RetryableLLMError
        except (openai.APIStatusError, anthropic.APIStatusError) as error:
            translated_error = (
                RetryableLLMError
                if _retryable_status(error.status_code)
                else NonRetryableLLMError
            )
        except (openai.APIError, anthropic.APIError):
            translated_error = NonRetryableLLMError

        if translated_error is not None:
            raise translated_error
        if self.kind == "anthropic":
            return _anthropic_response(response, self.model)
        return _openai_response(response, self.model)


class PrimaryReserveGateway:
    def __init__(
        self,
        primary: Provider,
        reserve: Provider | None = None,
    ) -> None:
        self.primary = primary
        self.reserve = reserve

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            return await self.primary.complete(request)
        except RetryableLLMError:
            if self.reserve is None:
                raise LLMUnavailable from None
        try:
            return await self.reserve.complete(request)
        except RetryableLLMError:
            raise LLMUnavailable from None
