"""PostgreSQL: история сообщений диалога."""

import asyncio
import logging

from config import DATABASE_URL, CONTEXT_MESSAGES_LIMIT, DATA_RETENTION_DAYS
from moroz.common.db import Database

logger = logging.getLogger(__name__)

_pool: Database | None = None
_pool_lock = asyncio.Lock()


async def init_db() -> None:
    global _pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан — без БД")
        return
    async with _pool_lock:
        if _pool is not None:
            return
        database = Database(DATABASE_URL, min_size=2, max_size=10)
        await database.connect()
        _pool = database
        logger.info("Пул подключений к БД создан")


async def close_db() -> None:
    global _pool
    async with _pool_lock:
        pool, _pool = _pool, None
        if pool:
            await pool.close()


async def _ensure_pool() -> bool:
    global _pool
    if _pool:
        return True
    if not DATABASE_URL:
        return False
    async with _pool_lock:
        if _pool:
            return True
        try:
            database = Database(DATABASE_URL, min_size=2, max_size=10)
            await database.connect()
        except Exception as error:
            logger.error("db_connect_failed error_type=%s", type(error).__name__)
            return False
        _pool = database
        return True


async def save_message(
    chat_id: int,
    user_id: int | None,
    role: str,
    content: str,
    username: str | None = None,
) -> None:
    if not await _ensure_pool():
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO messages (chat_id, user_id, username, role, content) "
                "VALUES ($1, $2, $3, $4, $5)",
                chat_id, user_id, username, role, content,
            )
    except Exception as error:
        logger.error("db_message_save_failed error_type=%s", type(error).__name__)


async def get_context(chat_id: int) -> list[dict[str, str]]:
    if not await _ensure_pool():
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM messages WHERE chat_id = $1 "
                "ORDER BY created_at DESC LIMIT $2",
                chat_id, CONTEXT_MESSAGES_LIMIT,
            )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    except Exception as error:
        logger.error("db_context_read_failed error_type=%s", type(error).__name__)
        return []


async def save_token_usage(
    chat_id: int,
    user_id: int | None,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    total_tokens: int,
    model: str,
) -> None:
    if not await _ensure_pool():
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO token_usage
                   (chat_id, user_id, prompt_tokens, completion_tokens,
                    cached_tokens, total_tokens, model)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                chat_id, user_id, prompt_tokens, completion_tokens,
                cached_tokens, total_tokens, model,
            )
    except Exception as error:
        logger.error(
            "db_token_usage_save_failed error_type=%s", type(error).__name__
        )


async def cleanup_old_records() -> dict[str, int]:
    """Удалить старые записи. Срок хранения — DATA_RETENTION_DAYS (дефолт 3 года).

    Если DATA_RETENTION_DAYS <= 0 — автоочистка выключена, возвращает {}.
    Запускать раз в сутки (см. _cleanup_loop в bot.py).
    """
    if DATA_RETENTION_DAYS <= 0:
        logger.info("Автоочистка БД выключена (DATA_RETENTION_DAYS=%d)", DATA_RETENTION_DAYS)
        return {}
    if not await _ensure_pool():
        return {}
    tables = ("messages", "token_usage")
    result = {}
    try:
        async with _pool.acquire() as conn:
            for table in tables:
                status = await conn.execute(
                    f"DELETE FROM {table} "
                    f"WHERE created_at < NOW() - ($1 || ' days')::INTERVAL",
                    str(DATA_RETENTION_DAYS),
                )
                result[table] = int(status.split()[-1])
        return result
    except Exception as error:
        logger.error("db_cleanup_failed error_type=%s", type(error).__name__)
        return {}
