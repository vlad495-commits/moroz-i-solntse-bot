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
from types import SimpleNamespace

from openai import AsyncOpenAI
import redis.asyncio as aioredis

from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_MAX_TOKENS,
    LLM_REQUEST_TIMEOUT_SEC,
    PROMPT_RELOAD_CHANNEL,
    REDIS_URL,
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
        return AsyncAnthropic(api_key=api_key, timeout=LLM_REQUEST_TIMEOUT_SEC)
    kwargs = {"api_key": api_key, "timeout": LLM_REQUEST_TIMEOUT_SEC}
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


def init_llm() -> None:
    """Инициализировать LLM-клиент. Один раз при старте."""
    global _primary_client, _primary_kind

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
    logger.info(
        "LLM-клиент создан: kind=%s, model=%s, base_url=%s",
        _primary_kind, LLM_MODEL, LLM_BASE_URL or "(default)",
    )


def _convert_messages_for_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-формат → Anthropic-формат. Извлекает system, чередует user/assistant."""
    system = ""
    msgs: list[dict] = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "") or ""
        if role == "system":
            system = content
        elif role == "user":
            msgs.append({"role": "user", "content": content})
        elif role == "assistant" and content:
            msgs.append({"role": "assistant", "content": content})

    cleaned: list[dict] = []
    for m in msgs:
        if cleaned and cleaned[-1]["role"] == m["role"]:
            cleaned[-1]["content"] += "\n" + m["content"]
        else:
            cleaned.append(m)
    if cleaned and cleaned[0]["role"] != "user":
        cleaned.insert(0, {"role": "user", "content": "Привет"})
    return system, cleaned


def _anthropic_to_openai_format(response) -> object:
    """Адаптировать ответ Anthropic к формату OpenAI."""
    text_blocks = [b.text for b in response.content if b.type == "text"]
    content = "\n".join(text_blocks) if text_blocks else None

    cached = getattr(response.usage, "cache_read_input_tokens", 0) or 0
    usage = SimpleNamespace(
        prompt_tokens=response.usage.input_tokens,
        completion_tokens=response.usage.output_tokens,
        cached_tokens=cached,
        total_tokens=response.usage.input_tokens + response.usage.output_tokens,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=usage,
        model=response.model,
    )


async def _invoke(messages: list[dict]) -> object:
    """Вызов LLM. Возвращает ответ в OpenAI-совместимом формате."""
    if _primary_kind == "anthropic":
        system, msgs = _convert_messages_for_anthropic(messages)
        response = await _primary_client.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=system,
            messages=msgs,
            temperature=LLM_TEMPERATURE,
        )
        return _anthropic_to_openai_format(response)

    return await _primary_client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
    )


async def generate_response(
    user_message: str,
    context: list[dict[str, str]],
) -> LLMResult:
    """Сгенерировать ответ LLM. Подкладывает системный промпт + контекст."""
    if not _primary_client:
        raise RuntimeError("LLM не инициализирован, вызовите init_llm()")

    messages: list[dict] = []
    if _system_prompt:
        messages.append({"role": "system", "content": _system_prompt})
    for msg in context:
        messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    response = await _invoke(messages)

    text = response.choices[0].message.content or ""
    usage = response.usage

    cached = 0
    if hasattr(usage, "prompt_tokens_details"):
        details = usage.prompt_tokens_details
        if details and hasattr(details, "cached_tokens"):
            cached = details.cached_tokens or 0
    elif hasattr(usage, "cached_tokens"):
        cached = usage.cached_tokens or 0

    return LLMResult(
        text=text,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cached_tokens=cached,
        total_tokens=usage.total_tokens,
        model=response.model or LLM_MODEL,
    )


async def prompt_reload_listener() -> None:
    """Listen for prompt reload events and reread system.md without restart."""
    backoff = 1.0
    while True:
        try:
            client = aioredis.from_url(REDIS_URL, decode_responses=True)
            pubsub = client.pubsub()
            await pubsub.subscribe(PROMPT_RELOAD_CHANNEL)
            logger.info("Prompt reload channel subscription active: %s", PROMPT_RELOAD_CHANNEL)
            backoff = 1.0
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                _load_prompt()
                logger.info(
                    "Prompt reloaded from disk: %d chars (trigger: %s)",
                    len(_system_prompt),
                    msg.get("data"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "prompt_reload_listener failed, retry in %.1f sec",
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
