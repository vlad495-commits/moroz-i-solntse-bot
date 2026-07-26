from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal, Protocol

import anthropic
import openai


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
    temperature: float
    max_tokens: int


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
    return status in {408, 409, 429} or status >= 500


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
    choices = getattr(response, "choices")
    text = choices[0].message.content or ""
    usage = getattr(response, "usage")
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
    total_tokens = int(
        getattr(usage, "total_tokens", prompt_tokens + completion_tokens)
        or 0
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
    content = getattr(response, "content")
    text = "\n".join(
        block.text for block in content if block.type == "text"
    )
    usage = getattr(response, "usage")
    prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return LLMResponse(
        text=text,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_tokens=int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
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
    ) -> None:
        self.client = client
        self.kind = kind
        self.model = model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            if self.kind == "anthropic":
                system, messages = _anthropic_messages(request.messages)
                response = await self.client.messages.create(
                    model=self.model,
                    max_tokens=request.max_tokens,
                    system=system,
                    messages=messages,
                    temperature=request.temperature,
                )
                return _anthropic_response(response, self.model)

            response = await self.client.chat.completions.create(
                model=self.model,
                messages=list(request.messages),
                temperature=request.temperature,
                max_tokens=request.max_tokens,
            )
            return _openai_response(response, self.model)
        except (
            openai.APITimeoutError,
            openai.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
        ):
            raise RetryableLLMError from None
        except (openai.APIStatusError, anthropic.APIStatusError) as error:
            if _retryable_status(error.status_code):
                raise RetryableLLMError from None
            raise NonRetryableLLMError from None
        except (openai.APIError, anthropic.APIError):
            raise NonRetryableLLMError from None


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
