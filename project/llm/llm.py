"""LLM-клиент: универсальный (любой провайдер).

Поддерживается:
- Anthropic (модели `claude-*`) — нативный AsyncAnthropic.
- Любой OpenAI-совместимый API — через AsyncOpenAI с custom base_url
  (OpenAI, OpenRouter, DeepSeek, Together AI, Groq, локальный Ollama/vLLM и т.д.).

Тип клиента определяется автоматически по модели и/или base_url:
- Если модель начинается с "claude" И base_url не задан → Anthropic native.
- Иначе → AsyncOpenAI (с указанным base_url или дефолтным openai.com).
"""

import asyncio
import logging
from dataclasses import dataclass

from openai import AsyncOpenAI
import redis.asyncio as aioredis
from moroz.security.llm_gateway import (
    LLMRequest,
    LLMResponse,
    PrimaryReserveGateway,
    SDKProvider,
)
from moroz.security.pipeline import SecurityPipeline
from moroz.security.validator import extract_structured_facts

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    LLM_REQUEST_TIMEOUT_SEC,
    PROMPT_RELOAD_CHANNEL,
    REDIS_URL,
    RESERVE_API_KEY,
    RESERVE_BASE_URL,
    RESERVE_MODEL,
    SYSTEM_PROMPT_PATH,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Результат вызова LLM: текст ответа + метрики токенов."""
    text: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    total_tokens: int
    model: str


_system_prompt: str = ""
_primary_client = None
_primary_kind: str = ""
_pipeline: SecurityPipeline | None = None
_pipeline_client = None


def _detect_kind(model: str, base_url: str | None) -> str:
    """Определить тип API по модели/base_url.

    Anthropic native — только если модель `claude-*` И base_url не задан явно.
    """
    if base_url:
        return "openai"
    if model.lower().startswith("claude") or "claude-" in model.lower():
        return "anthropic"
    return "openai"


def _create_client(api_key: str, base_url: str | None, kind: str):
    """Создать клиент нужного типа."""
    if kind == "anthropic":
        from anthropic import AsyncAnthropic
        return AsyncAnthropic(
            api_key=api_key,
            timeout=LLM_REQUEST_TIMEOUT_SEC,
            max_retries=0,
        )
    kwargs = {
        "api_key": api_key,
        "timeout": LLM_REQUEST_TIMEOUT_SEC,
        "max_retries": 0,
    }
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


def _load_prompt() -> None:
    """Перечитать system.md с диска. Идемпотентно."""
    global _system_prompt
    if SYSTEM_PROMPT_PATH.exists():
        _system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    else:
        _system_prompt = ""
    if _pipeline is not None:
        _pipeline.system_prompt = _system_prompt
        _pipeline.facts = extract_structured_facts(_system_prompt)


def init_llm() -> None:
    """Инициализировать LLM-клиент. Один раз при старте."""
    global _primary_client, _primary_kind, _pipeline, _pipeline_client

    _load_prompt()
    if not _system_prompt:
        logger.warning(
            "Системный промпт пустой. Запиши роль бота в %s", SYSTEM_PROMPT_PATH
        )
    else:
        logger.info("Системный промпт загружен: %d символов", len(_system_prompt))

    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY не задан в .env")
    _primary_kind = _detect_kind(LLM_MODEL, LLM_BASE_URL)
    _primary_client = _create_client(LLM_API_KEY, LLM_BASE_URL, _primary_kind)
    primary = SDKProvider(
        _primary_client,
        _primary_kind,
        LLM_MODEL,
        LLM_TEMPERATURE,
        LLM_MAX_TOKENS,
    )
    reserve = None
    if RESERVE_API_KEY and RESERVE_MODEL:
        reserve_kind = _detect_kind(RESERVE_MODEL, RESERVE_BASE_URL)
        reserve = SDKProvider(
            _create_client(RESERVE_API_KEY, RESERVE_BASE_URL, reserve_kind),
            reserve_kind,
            RESERVE_MODEL,
            LLM_TEMPERATURE,
            LLM_MAX_TOKENS,
        )
    _pipeline = SecurityPipeline(
        PrimaryReserveGateway(primary, reserve),
        _system_prompt,
        extract_structured_facts(_system_prompt),
    )
    _pipeline_client = _primary_client
    logger.info(
        "llm_client_created kind=%s model=%s custom_endpoint=%s",
        _primary_kind,
        LLM_MODEL,
        bool(LLM_BASE_URL),
    )


async def _invoke(messages: list[dict]) -> object:
    """Compatibility seam delegated to the audited SDK adapter."""
    return await SDKProvider(
        _primary_client,
        _primary_kind,
        LLM_MODEL,
        LLM_TEMPERATURE,
        LLM_MAX_TOKENS,
    ).complete(
        LLMRequest(
            messages=tuple(dict(message) for message in messages),
            purpose="legacy",
        )
    )


def _adapt_legacy_response(response: object) -> LLMResponse:
    if isinstance(response, LLMResponse):
        return response
    text = response.choices[0].message.content or ""
    usage = response.usage
    cached = 0
    if hasattr(usage, "prompt_tokens_details"):
        details = usage.prompt_tokens_details
        if details and hasattr(details, "cached_tokens"):
            cached = details.cached_tokens or 0
    elif hasattr(usage, "cached_tokens"):
        cached = usage.cached_tokens or 0
    return LLMResponse(
        text=text,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_tokens=cached,
        total_tokens=usage.total_tokens,
        model=response.model or LLM_MODEL,
    )


class _LegacyInvokeGateway:
    async def complete(self, request: LLMRequest) -> LLMResponse:
        messages = [dict(message) for message in request.messages]
        if messages and messages[0]["role"] == "system":
            messages[0]["content"] = messages[0]["content"].split(
                "\n\nROUTE intents=",
                1,
            )[0]
        response = await _invoke(messages)
        return _adapt_legacy_response(response)


async def generate_response(
    user_message: str,
    context: list[dict[str, str]],
    recent_message_count: int = 1,
) -> LLMResponse:
    """Сгенерировать ответ через общий security pipeline."""
    if not _primary_client:
        raise RuntimeError("LLM не инициализирован, вызовите init_llm()")

    active_pipeline = _pipeline
    if active_pipeline is None or _pipeline_client is not _primary_client:
        active_pipeline = SecurityPipeline(
            _LegacyInvokeGateway(),
            _system_prompt,
            extract_structured_facts(_system_prompt),
        )
    return await active_pipeline.respond(
        user_message,
        context,
        recent_message_count=recent_message_count,
    )


async def prompt_reload_listener() -> None:
    """Listen for prompt reload events and reread system.md without restart."""
    backoff = 1.0
    while True:
        client = None
        pubsub = None
        retry_delay = None
        try:
            client = aioredis.from_url(REDIS_URL, decode_responses=True)
            pubsub = client.pubsub()
            await pubsub.subscribe(PROMPT_RELOAD_CHANNEL)
            logger.info("prompt_reload_subscription_active")
            backoff = 1.0
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                _load_prompt()
                logger.info(
                    "prompt_reload_applied prompt_length=%d",
                    len(_system_prompt),
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "prompt_reload_listener_failed error_type=%s",
                type(error).__name__,
            )
            retry_delay = backoff
        finally:
            if pubsub is not None:
                try:
                    await pubsub.aclose()
                except Exception as error:
                    logger.error(
                        "prompt_reload_pubsub_close_failed error_type=%s",
                        type(error).__name__,
                    )
            if client is not None:
                try:
                    await client.aclose()
                except Exception as error:
                    logger.error(
                        "prompt_reload_redis_close_failed error_type=%s",
                        type(error).__name__,
                    )
        if retry_delay is not None:
            await asyncio.sleep(retry_delay)
            backoff = min(backoff * 2, 30.0)
