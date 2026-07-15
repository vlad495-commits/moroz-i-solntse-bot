"""Чтение статуса LLM-провайдеров из Redis.

Источник данных — ключи `llm:funds:{provider}`, которые llm.py обновляет
после каждого вызова: 'ok' при успехе, 'depleted' при insufficient_funds.
Реального числа баланса в долларах провайдеры не дают.
"""

import json
import logging
import os

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
PROVIDERS = ("main", "reserve")


async def get_llm_status() -> dict[str, dict]:
    """Вернуть статусы main/reserve. Если ключа нет — status='unknown'."""
    result: dict[str, dict] = {}
    client = None
    try:
        client = aioredis.from_url(REDIS_URL, decode_responses=True)
        for provider in PROVIDERS:
            raw = await client.get(f"llm:funds:{provider}")
            if raw:
                try:
                    result[provider] = json.loads(raw)
                except json.JSONDecodeError:
                    result[provider] = {"status": "unknown"}
            else:
                result[provider] = {"status": "unknown"}
    except Exception as error:
        logger.error(
            "llm_status_redis_failed error_type=%s", type(error).__name__
        )
        for provider in PROVIDERS:
            result.setdefault(provider, {"status": "unknown"})
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as error:
                logger.error(
                    "llm_status_redis_close_failed error_type=%s",
                    type(error).__name__,
                )

    # Признак, настроен ли резерв вообще
    result["reserve_configured"] = bool(
        os.getenv("RESERVE_API_KEY") and os.getenv("RESERVE_MODEL")
    )
    return result
