"""Чтение данных из общей PostgreSQL для админки.

Админка только читает, не пишет (кроме логина/сессии)."""

import logging
import os
from typing import Any

from moroz.common.db import Database
from moroz.common.config import database_url_from_env

logger = logging.getLogger(__name__)

DATABASE_URL = database_url_from_env(os.environ, required=False)

_pool: Database | None = None


async def init_db() -> None:
    global _pool
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан")
    if _pool is not None:
        return
    database = Database(DATABASE_URL, min_size=1, max_size=5)
    await database.connect()
    _pool = database
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


async def get_system_metrics_snapshot() -> dict[str, Any]:
    if not _pool:
        raise RuntimeError("database is not initialized")
    async with _pool.acquire() as conn:
        totals = await conn.fetchrow(
            """
            WITH inbox AS (
                SELECT
                    COUNT(*) AS inbound,
                    COUNT(*) FILTER (WHERE status = 'processed') AS processed,
                    COUNT(*) FILTER (WHERE status = 'accepted') AS accepted,
                    EXTRACT(EPOCH FROM (
                        now() - MIN(created_at)
                            FILTER (WHERE status = 'accepted')
                    )) AS oldest_age
                FROM message_inbox
            ),
            tasks AS (
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                    COUNT(*) FILTER (WHERE status = 'published') AS published
                FROM task_outbox
            ),
            usage AS (
                SELECT
                    COUNT(*) AS retained_calls,
                    COALESCE(SUM(total_tokens), 0) AS retained_tokens
                FROM token_usage
            ),
            escalation AS (
                SELECT COUNT(*) FILTER (WHERE status = 'open') AS open_count
                FROM escalations
            )
            SELECT
                inbox.inbound AS bot_inbound_messages_total,
                inbox.processed AS worker_processed_messages_total,
                inbox.accepted AS inbox_accepted_messages,
                inbox.oldest_age AS inbox_oldest_age_seconds,
                tasks.pending AS task_outbox_pending_messages,
                tasks.published AS task_outbox_published_total,
                usage.retained_calls AS retained_llm_calls,
                usage.retained_tokens AS retained_llm_tokens,
                escalation.open_count AS open_escalations
            FROM inbox
            CROSS JOIN tasks
            CROSS JOIN usage
            CROSS JOIN escalation
            """
        )
        outbound_rows = await conn.fetch(
            """
            SELECT status, COUNT(*) AS count
            FROM outbound_messages
            GROUP BY status
            """
        )
        scheduler_rows = await conn.fetch(
            """
            SELECT status, COUNT(*) AS count
            FROM scheduler_jobs
            GROUP BY status
            """
        )
    result = dict(totals)
    age = result["inbox_oldest_age_seconds"]
    result["inbox_oldest_age_seconds"] = (
        float(age) if age is not None else None
    )
    result["outbound_messages"] = {
        row["status"]: row["count"] for row in outbound_rows
    }
    result["scheduler_jobs"] = {
        row["status"]: row["count"] for row in scheduler_rows
    }
    return result


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
