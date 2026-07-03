"""Чтение данных из общей PostgreSQL для админки.

Админка только читает, не пишет (кроме логина/сессии)."""

import logging
import os
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    _pg_user = os.getenv("POSTGRES_USER", "")
    _pg_pass = os.getenv("POSTGRES_PASSWORD", "")
    _pg_db = os.getenv("POSTGRES_DB", "")
    if _pg_user and _pg_pass and _pg_db:
        DATABASE_URL = f"postgresql://{_pg_user}:{_pg_pass}@postgres:5432/{_pg_db}"

_pool: asyncpg.Pool | None = None


async def init_db() -> None:
    global _pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан")
    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
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
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_case_reviews (
                id BIGSERIAL PRIMARY KEY,
                case_id BIGINT REFERENCES eval_cases(id) ON DELETE CASCADE,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                reviewer VARCHAR(64),
                comment TEXT NOT NULL DEFAULT '',
                proposed_question TEXT,
                proposed_answer TEXT,
                category VARCHAR(64),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_case_reviews_case_id
            ON eval_case_reviews (case_id)
            WHERE case_id IS NOT NULL
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_eval_case_reviews_status
            ON eval_case_reviews (status, updated_at DESC)
        """)
    logger.info("Админка: пул подключений к БД создан")


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def get_chats_list(limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
    """Список чатов с агрегатами: count, last_message, токены, стоимость."""
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH chat_messages AS (
                SELECT
                    chat_id,
                    MAX(user_id) AS user_id,
                    MAX(username) AS username,
                    COUNT(*) AS message_count,
                    MAX(created_at) AS last_message_at
                FROM messages
                GROUP BY chat_id
            ),
            chat_tokens AS (
                SELECT
                    chat_id,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COUNT(*) AS llm_calls,
                    MAX(model) AS last_model
                FROM token_usage
                GROUP BY chat_id
            )
            SELECT
                cm.chat_id, cm.user_id, cm.username,
                cm.message_count, cm.last_message_at,
                COALESCE(ct.prompt_tokens, 0) AS prompt_tokens,
                COALESCE(ct.completion_tokens, 0) AS completion_tokens,
                COALESCE(ct.cached_tokens, 0) AS cached_tokens,
                COALESCE(ct.total_tokens, 0) AS total_tokens,
                COALESCE(ct.llm_calls, 0) AS llm_calls,
                ct.last_model
            FROM chat_messages cm
            LEFT JOIN chat_tokens ct ON cm.chat_id = ct.chat_id
            ORDER BY cm.last_message_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit, offset,
        )
    return [dict(r) for r in rows]


async def get_chats_total() -> int:
    if not _pool:
        return 0
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT COUNT(DISTINCT chat_id) AS n FROM messages")
    return row["n"] if row else 0


async def get_chat_detail(chat_id: int) -> dict[str, Any] | None:
    """Детали чата: все сообщения + токены/стоимость."""
    if not _pool:
        return None
    async with _pool.acquire() as conn:
        msg_rows = await conn.fetch(
            """
            SELECT id, chat_id, user_id, username, role, content, created_at
            FROM messages
            WHERE chat_id = $1
            ORDER BY created_at ASC
            """,
            chat_id,
        )
        if not msg_rows:
            return None

        token_row = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COUNT(*) AS llm_calls,
                MAX(model) AS last_model
            FROM token_usage
            WHERE chat_id = $1
            """,
            chat_id,
        )

    user_id = next((r["user_id"] for r in msg_rows if r["user_id"]), None)
    username = next((r["username"] for r in msg_rows if r["username"]), None)

    return {
        "chat_id": chat_id,
        "user_id": user_id,
        "username": username,
        "messages": [dict(r) for r in msg_rows],
        "stats": dict(token_row) if token_row else {},
    }


async def get_global_stats() -> dict[str, Any]:
    """Глобальная статистика: суммы по всему проекту."""
    if not _pool:
        return {}
    async with _pool.acquire() as conn:
        msg_stats = await conn.fetchrow(
            """
            SELECT
                COUNT(DISTINCT chat_id) AS total_chats,
                COUNT(DISTINCT user_id) AS total_users,
                COUNT(*) AS total_messages
            FROM messages
            """
        )
        token_stats = await conn.fetchrow(
            """
            SELECT
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COUNT(*) AS total_llm_calls
            FROM token_usage
            """
        )
        # security_incidents создаётся только на ступени 5 (guardrails).
        # До неё таблицы нет — to_regclass вернёт NULL без ошибки.
        has_incidents = await conn.fetchval(
            "SELECT to_regclass('public.security_incidents')"
        )
        total_incidents = 0
        if has_incidents:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS total_incidents FROM security_incidents"
            )
            total_incidents = row["total_incidents"]
    return {
        **dict(msg_stats),
        **dict(token_stats),
        "total_incidents": total_incidents,
    }


async def get_recent_incidents(limit: int = 20) -> list[dict[str, Any]]:
    """Последние инциденты безопасности (заблокированные сообщения).

    Таблица security_incidents создаётся на ступени 5 — до неё возвращаем [].
    """
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        has_incidents = await conn.fetchval(
            "SELECT to_regclass('public.security_incidents')"
        )
        if not has_incidents:
            return []
        rows = await conn.fetch(
            """
            SELECT id, chat_id, user_id, username, incident_type,
                   user_message, reason, created_at
            FROM security_incidents
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]
