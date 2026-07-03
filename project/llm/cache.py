"""Redis-кэш контекста диалога. Хранит последние N сообщений на чат."""

import json
import logging

import redis.asyncio as aioredis

from config import REDIS_URL, CONTEXT_MESSAGES_LIMIT

logger = logging.getLogger(__name__)

_redis: aioredis.Redis | None = None


def _key(chat_id: int) -> str:
    return f"chat:{chat_id}:messages"


async def init_cache() -> None:
    global _redis
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    await _redis.ping()
    logger.info("Redis подключён: %s", REDIS_URL)


async def close_cache() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


async def _ensure_redis() -> bool:
    global _redis
    try:
        if _redis:
            await _redis.ping()
            return True
    except Exception:
        logger.warning("Redis: соединение потеряно, переподключаюсь...")

    try:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        await _redis.ping()
        logger.info("Redis: соединение восстановлено")
        return True
    except Exception:
        logger.exception("Redis недоступен")
        _redis = None
        return False


async def get_redis() -> aioredis.Redis | None:
    if not await _ensure_redis():
        return None
    return _redis


async def push_message(chat_id: int, role: str, content: str) -> None:
    """Добавить сообщение в контекст чата (RPUSH + LTRIM до CONTEXT_MESSAGES_LIMIT)."""
    if not await _ensure_redis():
        return
    try:
        key = _key(chat_id)
        msg = json.dumps({"role": role, "content": content}, ensure_ascii=False)
        async with _redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, msg)
            pipe.ltrim(key, -CONTEXT_MESSAGES_LIMIT, -1)
            await pipe.execute()
    except Exception:
        logger.exception("Ошибка записи в Redis для чата %s", chat_id)


async def get_context(chat_id: int) -> list[dict[str, str]]:
    """Последние N сообщений в хронологическом порядке."""
    if not await _ensure_redis():
        return []
    try:
        raw = await _redis.lrange(_key(chat_id), 0, -1)
        return [json.loads(item) for item in raw]
    except Exception:
        logger.exception("Ошибка чтения контекста из Redis")
        return []
