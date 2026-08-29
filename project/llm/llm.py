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
import hashlib
import json
import logging
import re
from dataclasses import dataclass

from openai import AsyncOpenAI
import redis.asyncio as aioredis
from moroz.messaging.router import LLMIntentRouter
from moroz.security.context_compactor import ContextCompactor
from moroz.security.input_security import LLMInputSecurityClassifier
from moroz.security.llm_gateway import (
    LLMRequest,
    LLMResponse,
    PrimaryReserveGateway,
    SDKProvider,
)
from moroz.security.pipeline import SecurityPipeline
from moroz.security.output_validator import LLMOutputValidator
from moroz.security.validator import extract_structured_facts

from config import (
    COMPACT_API_KEY,
    COMPACT_BASE_URL,
    COMPACT_KEEP_RECENT,
    COMPACT_MAX_TOKENS,
    COMPACT_MODEL,
    COMPACT_THRESHOLD,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    LLM_REQUEST_TIMEOUT_SEC,
    OUTPUT_VALIDATOR_ENABLED,
    PROMPT_RELOAD_CHANNEL,
    REDIS_URL,
    RESERVE_API_KEY,
    RESERVE_BASE_URL,
    RESERVE_MODEL,
    ROUTER_API_KEY,
    ROUTER_BASE_URL,
    ROUTER_MAX_TOKENS,
    ROUTER_MODEL,
    SECURITY_API_KEY,
    SECURITY_BASE_URL,
    SECURITY_MAX_TOKENS,
    SECURITY_MODEL,
    SYSTEM_PROMPT_PATH,
)

logger = logging.getLogger(__name__)
PROMPT_RELOAD_ACK_PREFIX = "prompt:reload:ack:"
PROMPT_RELOAD_ACK_TTL_SECONDS = 30
PROMPT_RELOAD_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")
PROMPT_RELOAD_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


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


def _load_prompt(expected_sha256: str | None = None) -> None:
    """Build and atomically install a complete prompt pipeline snapshot."""
    global _system_prompt, _pipeline
    raw_candidate = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    if expected_sha256 is not None:
        actual_sha256 = hashlib.sha256(raw_candidate.encode("utf-8")).hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError("prompt content does not match reload version")
    candidate = raw_candidate.strip()
    if not candidate:
        raise ValueError("system prompt is empty")
    facts = extract_structured_facts(candidate)
    candidate_pipeline = None
    if _pipeline is not None:
        candidate_pipeline = SecurityPipeline(
            _pipeline.gateway,
            candidate,
            facts,
            router=getattr(_pipeline, "router", None),
            input_security=getattr(_pipeline, "input_security", None),
            output_validator=getattr(_pipeline, "output_validator", None),
            context_compactor=getattr(_pipeline, "context_compactor", None),
        )

    _system_prompt = candidate
    if candidate_pipeline is not None:
        _pipeline = candidate_pipeline


def _parse_prompt_reload(payload: str) -> tuple[str, str]:
    message = json.loads(payload)
    if not isinstance(message, dict) or not isinstance(
        message.get("version_id"), int
    ):
        raise ValueError("invalid prompt reload payload")
    request_id = message.get("request_id")
    digest = message.get("sha256")
    if not isinstance(request_id, str) or not PROMPT_RELOAD_REQUEST_ID_RE.fullmatch(
        request_id
    ):
        raise ValueError("invalid prompt reload request id")
    if not isinstance(digest, str) or not PROMPT_RELOAD_DIGEST_RE.fullmatch(digest):
        raise ValueError("invalid prompt reload digest")
    return f"{PROMPT_RELOAD_ACK_PREFIX}{request_id}", digest


async def _set_prompt_reload_ack(client, ack_key: str, value: str) -> None:
    try:
        await client.set(
            ack_key,
            value,
            ex=PROMPT_RELOAD_ACK_TTL_SECONDS,
        )
    except Exception as error:
        logger.error(
            "prompt_reload_ack_failed error_type=%s",
            type(error).__name__,
        )


async def _process_prompt_reload(client, payload: str) -> bool:
    try:
        ack_key, expected_sha256 = _parse_prompt_reload(payload)
    except Exception as error:
        logger.error(
            "prompt_reload_rejected error_type=%s",
            type(error).__name__,
        )
        return False

    try:
        _load_prompt(expected_sha256)
    except Exception as error:
        await _set_prompt_reload_ack(client, ack_key, "rejected")
        logger.error(
            "prompt_reload_rejected error_type=%s",
            type(error).__name__,
        )
        return False

    await _set_prompt_reload_ack(client, ack_key, "applied")
    logger.info(
        "prompt_reload_applied prompt_length=%d",
        len(_system_prompt),
    )
    return True


def init_llm(
    security_alert=None,
    output_alert=None,
    compact_alert=None,
) -> None:
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
    router_kind = _detect_kind(ROUTER_MODEL, ROUTER_BASE_URL)
    router_provider = SDKProvider(
        _create_client(ROUTER_API_KEY, ROUTER_BASE_URL, router_kind),
        router_kind,
        ROUTER_MODEL,
        0.0,
        ROUTER_MAX_TOKENS,
    )
    security_kind = _detect_kind(SECURITY_MODEL, SECURITY_BASE_URL)
    security_provider = SDKProvider(
        _create_client(SECURITY_API_KEY, SECURITY_BASE_URL, security_kind),
        security_kind,
        SECURITY_MODEL,
        0.0,
        SECURITY_MAX_TOKENS,
    )
    compact_kind = _detect_kind(COMPACT_MODEL, COMPACT_BASE_URL)
    compact_provider = SDKProvider(
        _create_client(COMPACT_API_KEY, COMPACT_BASE_URL, compact_kind),
        compact_kind,
        COMPACT_MODEL,
        0.0,
        COMPACT_MAX_TOKENS,
    )
    gateway = PrimaryReserveGateway(primary, reserve)
    _pipeline = SecurityPipeline(
        gateway,
        _system_prompt,
        extract_structured_facts(_system_prompt),
        router=LLMIntentRouter(router_provider),
        input_security=LLMInputSecurityClassifier(
            security_provider,
            reserve,
            security_alert,
        ),
        output_validator=(
            LLMOutputValidator(gateway, output_alert)
            if OUTPUT_VALIDATOR_ENABLED
            else None
        ),
        context_compactor=ContextCompactor(
            compact_provider,
            compact_alert,
            threshold=COMPACT_THRESHOLD,
            keep_recent=COMPACT_KEEP_RECENT,
        ),
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
    catalog=None,
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
        catalog=catalog,
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
                await _process_prompt_reload(client, msg.get("data", ""))
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
