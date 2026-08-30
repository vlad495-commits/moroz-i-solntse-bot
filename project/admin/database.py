"""Чтение данных из общей PostgreSQL для админки.

Админка только читает, не пишет (кроме логина/сессии)."""

import base64
import binascii
import json
import logging
import os
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from moroz.common.db import Database
from moroz.common.config import database_url_from_env
from moroz.escalation.service import ADMIN_REPLY_PREFIX, admin_reply_key
from moroz.messaging.repository import MessageRepository
from moroz.privacy import customer_lock_subject
from customer_events import (
    normalize_customer_event,
    safe_handoff_reason,
    safe_handoff_source,
)

logger = logging.getLogger(__name__)

DATABASE_URL = database_url_from_env(os.environ, required=False)

_pool: Database | None = None


def get_database() -> Database | None:
    return _pool


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


async def get_open_escalations(limit: int = 100) -> list[dict[str, Any]]:
    """Return a bounded, safe presentation of open human handoffs."""
    if not 1 <= limit <= 100:
        raise ValueError("open escalations limit")
    if not _pool:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT e.id, e.customer_id, e.source, e.reason_code, e.created_at,
                   COALESCE(h.enabled, false) AS human_mode_enabled
            FROM escalations AS e
            LEFT JOIN human_mode AS h ON h.customer_id = e.customer_id
            WHERE e.status = 'open'
            ORDER BY e.created_at DESC, e.id DESC
            LIMIT $1
            """,
            limit,
        )
    return [
        {
            "id": row["id"],
            "customer_id": row["customer_id"],
            "source": safe_handoff_source(row["source"]),
            "reason": safe_handoff_reason(row["reason_code"]),
            "created_at": row["created_at"],
            "human_mode_enabled": row["human_mode_enabled"],
        }
        for row in rows
    ]


async def enqueue_escalation_reply(
    escalation_id: UUID,
    *,
    reply_token: UUID,
    text: str,
    actor_id: int,
    ip_address: str | None,
    user_agent: str | None,
) -> tuple[str, UUID | None]:
    """Atomically queue one staff reply without calling its transport."""
    if not text or len(text) > 4096:
        raise ValueError("escalation reply text")
    if not _pool:
        raise RuntimeError("database unavailable")
    key = admin_reply_key(escalation_id, reply_token)
    repository = MessageRepository(_pool)
    async with _pool.acquire() as conn:
        async with conn.transaction():
            customer_id = await conn.fetchval(
                "SELECT customer_id FROM escalations WHERE id = $1",
                escalation_id,
            )
            if customer_id is None:
                return "not_found", None
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                customer_lock_subject(customer_id),
            )
            escalation = await conn.fetchrow(
                """
                SELECT customer_id, status
                FROM escalations
                WHERE id = $1
                FOR UPDATE
                """,
                escalation_id,
            )
            if escalation is None:
                return "not_found", None
            existing_id = await conn.fetchval(
                "SELECT id FROM outbound_messages WHERE idempotency_key = $1",
                key,
            )
            if existing_id is not None:
                return "already_queued", existing_id
            mode = await conn.fetchrow(
                """
                SELECT enabled
                FROM human_mode
                WHERE customer_id = $1
                FOR UPDATE
                """,
                customer_id,
            )
            if escalation["status"] != "open" or mode is None or not mode["enabled"]:
                return "inactive", None
            pending_id = await conn.fetchval(
                """
                SELECT id
                FROM outbound_messages
                WHERE left(idempotency_key, length($1)) = $1
                  AND status IN ('pending', 'sending')
                ORDER BY created_at, id
                LIMIT 1
                """,
                f"{ADMIN_REPLY_PREFIX}:{escalation_id}:",
            )
            if pending_id is not None:
                return "already_queued", pending_id
            outbound_id = await repository.enqueue_outbound_in_transaction(
                conn,
                channel="telegram",
                chat_id=customer_id,
                text=text,
                idempotency_key=key,
            )
            await conn.execute(
                """
                INSERT INTO admin_audit_events (
                    actor_id, action, object_type, object_id,
                    before, after, ip_address, user_agent
                )
                VALUES (
                    $1, 'escalation.reply_queued', 'escalation', $2,
                    NULL, $3::jsonb, $4, $5
                )
                """,
                actor_id,
                str(escalation_id),
                json.dumps(
                    {"outbound_id": str(outbound_id), "status": "queued"},
                    ensure_ascii=False,
                ),
                ip_address,
                user_agent,
            )
    return "queued", outbound_id


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


async def get_customer_events(
    chat_id: int,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return one safe, newest-first page of existing customer events."""
    if not 1 <= limit <= 50:
        raise ValueError("customer events page bounds")
    boundary = _decode_customer_events_cursor(cursor)
    empty = {
        "items": [],
        "next_cursor": None,
        "has_more": False,
    }
    if not _pool:
        return empty
    customer_id = str(chat_id)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH customer_events AS (
                SELECT
                    'message'::text AS source,
                    message.id::text AS source_id,
                    message.created_at AS occurred_at,
                    ('message.' || message.role)::text AS kind,
                    message.content::text AS description,
                    NULL::text AS status
                FROM messages AS message
                WHERE message.chat_id = $1::bigint

                UNION ALL

                SELECT
                    'booking', event.id::text, event.created_at,
                    'booking.' || event.event_type,
                    NULL::text, scenario.phase
                FROM booking_events AS event
                JOIN booking_scenarios AS scenario
                  ON scenario.id = event.scenario_id
                WHERE scenario.customer_id = $2

                UNION ALL

                SELECT
                    'scheduler', job.id::text || ':scheduled', job.created_at,
                    'scheduler.scheduled', job.kind, 'pending'
                FROM scheduler_jobs AS job
                WHERE CASE
                    WHEN jsonb_typeof(job.payload->'customer_id') = 'string'
                    THEN job.payload->>'customer_id' = $2
                    ELSE EXISTS (
                        SELECT 1 FROM bookings AS booking
                        WHERE booking.booking_key = job.booking_key
                          AND booking.customer_id = $2
                    ) END

                UNION ALL

                SELECT
                    'scheduler', job.id::text || ':terminal', job.finished_at,
                    'scheduler.' || job.status, job.kind, job.status
                FROM scheduler_jobs AS job
                WHERE job.finished_at IS NOT NULL
                  AND CASE
                    WHEN jsonb_typeof(job.payload->'customer_id') = 'string'
                    THEN job.payload->>'customer_id' = $2
                    ELSE EXISTS (
                        SELECT 1 FROM bookings AS booking
                        WHERE booking.booking_key = job.booking_key
                          AND booking.customer_id = $2
                    ) END

                UNION ALL

                SELECT
                    'escalation', escalation.id::text || ':opened',
                    escalation.created_at, 'handoff.opened',
                    escalation.reason_code, 'open'
                FROM escalations AS escalation
                WHERE escalation.customer_id = $2

                UNION ALL

                SELECT
                    'escalation', escalation.id::text || ':resolved',
                    escalation.resolved_at, 'handoff.resolved',
                    escalation.reason_code, 'resolved'
                FROM escalations AS escalation
                WHERE escalation.customer_id = $2
                  AND escalation.resolved_at IS NOT NULL

                UNION ALL

                SELECT
                    'human_mode', mode.customer_id, mode.enabled_at,
                    'handoff.enabled', mode.reason_code, 'enabled'
                FROM human_mode AS mode
                WHERE mode.customer_id = $2 AND mode.enabled = true

                UNION ALL

                SELECT
                    'admin', audit.id::text, audit.created_at,
                    'admin.' || audit.action, NULL::text, NULL::text
                FROM admin_audit_events AS audit
                WHERE audit.object_type = 'customer'
                  AND audit.object_id = $2

                UNION ALL

                SELECT
                    'admin', audit.id::text, audit.created_at,
                    'admin.' || audit.action, NULL::text,
                    CASE audit.action
                        WHEN 'escalation.reply_queued' THEN 'queued'
                        WHEN 'escalation.reply_delivered' THEN 'delivered'
                    END
                FROM admin_audit_events AS audit
                JOIN escalations AS escalation
                  ON audit.object_type = 'escalation'
                 AND audit.object_id = escalation.id::text
                WHERE escalation.customer_id = $2
                  AND audit.action IN (
                      'escalation.reply_queued',
                      'escalation.reply_delivered'
                  )
            )
            SELECT source, source_id, occurred_at, kind, description, status
            FROM customer_events
            WHERE $3::timestamptz IS NULL
               OR (occurred_at, source, source_id)
                    < ($3::timestamptz, $4::text, $5::text)
            ORDER BY occurred_at DESC, source DESC, source_id DESC
            LIMIT $6
            """,
            chat_id,
            customer_id,
            boundary[0] if boundary else None,
            boundary[1] if boundary else None,
            boundary[2] if boundary else None,
            limit + 1,
        )
    has_more = len(rows) > limit
    page_rows = list(rows[:limit])
    items = [normalize_customer_event(row) for row in page_rows]
    next_cursor = None
    if page_rows and has_more:
        last = page_rows[-1]
        next_cursor = _encode_customer_events_cursor(
            last["occurred_at"],
            last["source"],
            last["source_id"],
        )
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


def _encode_customer_events_cursor(
    occurred_at: datetime,
    source: str,
    source_id: str,
) -> str:
    payload = json.dumps(
        [occurred_at.isoformat(), source, source_id],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_customer_events_cursor(
    cursor: str | None,
) -> tuple[datetime, str, str] | None:
    if cursor is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values = json.loads(raw)
        if (
            not isinstance(values, list)
            or len(values) != 3
            or not all(isinstance(value, str) for value in values)
        ):
            raise ValueError
        occurred_at = datetime.fromisoformat(values[0])
        if occurred_at.tzinfo is None:
            raise ValueError
        return occurred_at, values[1], values[2]
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as error:
        raise ValueError("customer events cursor") from error


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


async def get_statistics_snapshot(period) -> dict[str, Any]:
    if not _pool:
        raise RuntimeError("database is not initialized")
    async with _pool.acquire() as conn:
        totals = await conn.fetchrow(
            """
            WITH message_totals AS (
                SELECT
                    COUNT(DISTINCT user_id) FILTER (
                        WHERE role = 'user' AND user_id IS NOT NULL
                    ) AS users,
                    COUNT(*) AS messages
                FROM messages
                WHERE created_at >= $1 AND created_at < $2
            ),
            automatic AS (
                SELECT COUNT(*) AS automatic_replies
                FROM outbound_messages
                WHERE created_at >= $1 AND created_at < $2
                  AND status = 'sent'
                  AND idempotency_key LIKE 'reply:%'
            ),
            automated AS (
                SELECT COUNT(DISTINCT bot.chat_id) AS automated_dialogues
                FROM outbound_messages AS bot
                WHERE bot.created_at >= $1 AND bot.created_at < $2
                  AND bot.status = 'sent'
                  AND bot.idempotency_key LIKE 'reply:%'
                  AND NOT EXISTS (
                      SELECT 1 FROM escalations AS escalation
                      WHERE escalation.customer_id = bot.chat_id
                        AND escalation.created_at >= $1
                        AND escalation.created_at < $2
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM outbound_messages AS staff
                      WHERE staff.chat_id = bot.chat_id
                        AND staff.created_at >= $1
                        AND staff.created_at < $2
                        AND staff.status = 'sent'
                        AND staff.idempotency_key LIKE 'admin_handoff_reply:%'
                  )
            ),
            escalation_totals AS (
                SELECT COUNT(*) AS escalations
                FROM escalations
                WHERE created_at >= $1 AND created_at < $2
            ),
            usage_totals AS (
                SELECT
                    COUNT(*) AS llm_calls,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens
                FROM token_usage
                WHERE created_at >= $1 AND created_at < $2
            )
            SELECT *
            FROM message_totals
            CROSS JOIN automatic
            CROSS JOIN automated
            CROSS JOIN escalation_totals
            CROSS JOIN usage_totals
            """,
            period.starts_at,
            period.ends_at,
        )
        usage_rows = await conn.fetch(
            """
            SELECT
                model,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(cached_tokens), 0) AS cached_tokens
            FROM token_usage
            WHERE created_at >= $1 AND created_at < $2
            GROUP BY model
            ORDER BY model
            """,
            period.starts_at,
            period.ends_at,
        )
        has_incidents = await conn.fetchval(
            "SELECT to_regclass('public.security_incidents')"
        )
        incidents = None
        incidents_reason = "Нет данных: Security-инциденты ещё не сохраняются."
        if has_incidents:
            incidents = await conn.fetchval(
                """
                SELECT COUNT(*) FROM security_incidents
                WHERE created_at >= $1 AND created_at < $2
                """,
                period.starts_at,
                period.ends_at,
            )
            incidents_reason = None
    result = dict(totals)
    result["usage_rows"] = [dict(row) for row in usage_rows]
    result["security_incidents"] = incidents
    result["security_incidents_reason"] = incidents_reason
    return result


async def get_statistics_settings() -> dict[str, Decimal | None]:
    empty = {"minutes_per_dialogue": None, "hourly_rate_rub": None}
    if not _pool:
        return empty
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT minutes_per_dialogue, hourly_rate_rub
            FROM admin_statistics_settings
            WHERE id = true
            """
        )
    return dict(row) if row else empty


async def save_statistics_settings(
    minutes_per_dialogue: Decimal,
    hourly_rate_rub: Decimal,
) -> dict[str, Decimal]:
    if minutes_per_dialogue <= 0 or hourly_rate_rub <= 0:
        raise ValueError("statistics settings")
    if not _pool:
        raise RuntimeError("database is not initialized")
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO admin_statistics_settings
                (id, minutes_per_dialogue, hourly_rate_rub, updated_at)
            VALUES (true, $1, $2, now())
            ON CONFLICT (id) DO UPDATE SET
                minutes_per_dialogue = EXCLUDED.minutes_per_dialogue,
                hourly_rate_rub = EXCLUDED.hourly_rate_rub,
                updated_at = now()
            RETURNING minutes_per_dialogue, hourly_rate_rub
            """,
            minutes_per_dialogue,
            hourly_rate_rub,
        )
    return dict(row)


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
