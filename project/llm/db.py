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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                model VARCHAR(64) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_token_usage_chat_created
            ON token_usage (chat_id, created_at DESC)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id BIGSERIAL PRIMARY KEY,
                content TEXT NOT NULL,
                author VARCHAR(64),
                comment TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prompt_versions_created
            ON prompt_versions (created_at DESC)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_cases (
                id BIGSERIAL PRIMARY KEY,
                category VARCHAR(64) NOT NULL DEFAULT 'general',
                question TEXT NOT NULL,
                expected_keywords TEXT[] NOT NULL DEFAULT '{}',
                forbidden_keywords TEXT[] NOT NULL DEFAULT '{}',
                expected_answer TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_runs (
                id BIGSERIAL PRIMARY KEY,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                total INTEGER NOT NULL DEFAULT 0,
                passed INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL DEFAULT 'running',
                judge_model VARCHAR(64),
                error_message TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_results (
                id BIGSERIAL PRIMARY KEY,
                run_id BIGINT NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
                case_id BIGINT REFERENCES eval_cases(id) ON DELETE SET NULL,
                question TEXT NOT NULL,
                expected_answer TEXT NOT NULL,
                actual_answer TEXT,
                verdict VARCHAR(32) NOT NULL,
                check_layer VARCHAR(16),
                score REAL,
                judge_reasoning TEXT,
                duration_ms INTEGER,
                error_message TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_eval_results_run
            ON eval_results (run_id, id)
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
    except Exception:
        logger.exception("Ошибка сохранения token_usage")


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
    except Exception:
        logger.exception("Ошибка автоочистки")
        return {}
