"""PostgreSQL: история сообщений диалога."""

import logging

import asyncpg

from config import DATABASE_URL, CONTEXT_MESSAGES_LIMIT, DATA_RETENTION_DAYS

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    global _pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL не задан — без БД")
        return
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT,
                username VARCHAR(255),
                role VARCHAR(16) NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                answered BOOLEAN NOT NULL DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_chat_created
            ON messages (chat_id, created_at DESC)
        """)

        # --- Миграции существующих таблиц ---
        # Правило: новую колонку добавляй И в CREATE TABLE выше (для новых БД),
        # И сюда через ADD COLUMN IF NOT EXISTS (для уже работающих прод-БД,
        # где таблица создана раньше — CREATE TABLE IF NOT EXISTS её не тронет).
        await conn.execute(
            "ALTER TABLE messages "
            "ADD COLUMN IF NOT EXISTS answered BOOLEAN NOT NULL DEFAULT FALSE"
        )
    logger.info("БД инициализирована")


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def _ensure_pool() -> bool:
    global _pool
    if _pool:
        return True
    if not DATABASE_URL:
        return False
    try:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        return True
    except Exception:
        logger.exception("PostgreSQL недоступен")
        _pool = None
        return False


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
    except Exception:
        logger.exception("Ошибка сохранения сообщения")


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
    except Exception:
        logger.exception("Ошибка чтения контекста")
        return []


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
    result = {}
    try:
        async with _pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM messages "
                "WHERE created_at < NOW() - ($1 || ' days')::INTERVAL",
                str(DATA_RETENTION_DAYS),
            )
            result["messages"] = int(status.split()[-1])
        return result
    except Exception:
        logger.exception("Ошибка автоочистки")
        return {}
