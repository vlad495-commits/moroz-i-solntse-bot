"""Owner-triggered removal of one Telegram customer's local data."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from moroz.privacy import (
    DELETION_MARKER_TTL_SECONDS,
    DELETION_OPERATION_TIMEOUT_SECONDS,
    customer_lock_subject,
    deletion_lock_key,
    deletion_marker_key,
)


DELETION_CHANNEL = "telegram"


class CustomerDataDeletionError(RuntimeError):
    """Safe public failure without customer or infrastructure details."""


@dataclass(frozen=True)
class DeletionResult:
    status: Literal["deleted", "already_absent"]
    deleted_counts: dict[str, int]


def _delete_count(command_tag: str) -> int:
    parts = command_tag.split()
    if len(parts) != 2 or parts[0] != "DELETE" or not parts[1].isdigit():
        raise CustomerDataDeletionError("customer data deletion failed")
    return int(parts[1])


async def _delete_owned_marker(redis_client, marker: str, token: str) -> None:
    await redis_client.eval(
        """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('del', KEYS[1])
        end
        return 0
        """,
        1,
        marker,
        token,
    )


async def _clear_redis(redis_client, chat_id: str, user_ids: set[str]) -> None:
    pipe = redis_client.pipeline(transaction=True)
    pipe.delete(
        f"chat:{chat_id}:messages",
        f"buffer:{chat_id}",
    )
    pipe.zrem("buffer:deadlines", chat_id)
    for user_id in sorted(user_ids):
        pipe.delete(f"consent:state:telegram:{chat_id}:{user_id}")
    await pipe.execute()


async def _delete(conn, counts: dict[str, int], name: str, query: str, *args) -> None:
    counts[name] = _delete_count(await conn.execute(query, *args))


async def _delete_customer_data(
    *,
    pool,
    redis_client,
    chat_id: int,
    actor_id: int,
    ip_address: str | None,
    user_agent: str | None,
) -> DeletionResult:
    chat = str(chat_id)
    marker = deletion_marker_key(DELETION_CHANNEL, chat)
    marker_token = uuid4().hex
    privacy_lock = None
    buffer_lock = None
    marker_set = False
    privacy_lock_acquired = False
    buffer_lock_acquired = False
    try:
        privacy_lock = redis_client.lock(
            deletion_lock_key(DELETION_CHANNEL, chat),
            timeout=DELETION_MARKER_TTL_SECONDS,
            blocking_timeout=1,
        )
        buffer_lock = redis_client.lock(
            f"lock:buffer:{chat}",
            timeout=DELETION_MARKER_TTL_SECONDS,
            blocking_timeout=2,
        )
        if not await privacy_lock.acquire():
            raise CustomerDataDeletionError("customer data deletion failed")
        privacy_lock_acquired = True
        await redis_client.set(
            marker,
            marker_token,
            ex=DELETION_MARKER_TTL_SECONDS,
        )
        marker_set = True
        if not await buffer_lock.acquire():
            raise CustomerDataDeletionError("customer data deletion failed")
        buffer_lock_acquired = True
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout = '10s'")
                await conn.execute("SET LOCAL statement_timeout = '30s'")
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    customer_lock_subject(chat),
                )

                user_ids = {chat}
                user_rows = await conn.fetch(
                    """
                    SELECT DISTINCT user_id::text AS user_id
                    FROM messages
                    WHERE chat_id = $1 AND user_id IS NOT NULL
                    UNION
                    SELECT DISTINCT payload->>'user_id' AS user_id
                    FROM message_inbox
                    WHERE channel = $2 AND chat_id = $3
                      AND payload->>'user_id' IS NOT NULL
                    """,
                    chat_id,
                    DELETION_CHANNEL,
                    chat,
                )
                user_ids.update(row["user_id"] for row in user_rows)
                update_ids = [
                    row["external_message_id"]
                    for row in await conn.fetch(
                        """
                        SELECT external_message_id FROM message_inbox
                        WHERE channel = $1 AND chat_id = $2
                        """,
                        DELETION_CHANNEL,
                        chat,
                    )
                ]
                scenarios = [
                    row["id"]
                    for row in await conn.fetch(
                        "SELECT id FROM booking_scenarios WHERE customer_id = $1",
                        chat,
                    )
                ]
                booking_keys = [
                    row["booking_key"]
                    for row in await conn.fetch(
                        "SELECT booking_key FROM bookings WHERE customer_id = $1",
                        chat,
                    )
                ]
                outbound_ids = [
                    str(row["id"])
                    for row in await conn.fetch(
                        """
                        SELECT id FROM outbound_messages
                        WHERE (channel = $1 AND chat_id = $2)
                           OR split_part(idempotency_key, ':', 2)
                              = ANY($3::text[])
                        """,
                        DELETION_CHANNEL,
                        chat,
                        [str(value) for value in booking_keys],
                    )
                ]

                await _clear_redis(redis_client, chat, user_ids)

                counts: dict[str, int] = {}
                await _delete(
                    conn,
                    counts,
                    "booking_events",
                    "DELETE FROM booking_events WHERE scenario_id = ANY($1::uuid[])",
                    scenarios,
                )
                await _delete(
                    conn,
                    counts,
                    "scheduler_jobs",
                    """
                    DELETE FROM scheduler_jobs
                    WHERE booking_key = ANY($1::uuid[])
                       OR payload->>'customer_id' = $2
                    """,
                    booking_keys,
                    chat,
                )
                await _delete(
                    conn, counts, "notification_feedback_requests",
                    "DELETE FROM notification_feedback_requests WHERE customer_id = $1",
                    chat,
                )
                await _delete(
                    conn, counts, "bookings",
                    "DELETE FROM bookings WHERE customer_id = $1",
                    chat,
                )
                await _delete(
                    conn, counts, "booking_scenarios",
                    "DELETE FROM booking_scenarios WHERE customer_id = $1",
                    chat,
                )
                await _delete(
                    conn, counts, "escalations",
                    "DELETE FROM escalations WHERE customer_id = $1",
                    chat,
                )
                await _delete(
                    conn, counts, "human_mode",
                    "DELETE FROM human_mode WHERE customer_id = $1",
                    chat,
                )
                await _delete(
                    conn,
                    counts,
                    "task_outbox",
                    """
                    DELETE FROM task_outbox
                    WHERE payload->>'chat_id' = $1
                       OR payload->>'user_id' = ANY($2::text[])
                       OR payload->>'customer_id' = $1
                       OR payload->>'outbound_id' = ANY($3::text[])
                       OR EXISTS (
                           SELECT 1 FROM jsonb_array_elements_text(
                               COALESCE(payload->'update_ids', '[]'::jsonb)
                           ) AS item(value)
                           WHERE item.value = ANY($4::text[])
                       )
                    """,
                    chat,
                    sorted(user_ids),
                    outbound_ids,
                    update_ids,
                )
                await _delete(
                    conn, counts, "outbound_messages",
                    "DELETE FROM outbound_messages "
                    "WHERE id::text = ANY($1::text[])",
                    outbound_ids,
                )
                await _delete(
                    conn, counts, "message_inbox",
                    "DELETE FROM message_inbox WHERE channel = $1 AND chat_id = $2",
                    DELETION_CHANNEL,
                    chat,
                )
                await _delete(
                    conn, counts, "processing_consents",
                    "DELETE FROM processing_consents WHERE channel = $1 AND user_id = ANY($2::text[])",
                    DELETION_CHANNEL,
                    sorted(user_ids),
                )
                await _delete(
                    conn, counts, "token_usage",
                    "DELETE FROM token_usage WHERE chat_id = $1 OR user_id::text = ANY($2::text[])",
                    chat_id,
                    sorted(user_ids),
                )
                if await conn.fetchval(
                    "SELECT to_regclass('public.security_incidents')"
                ):
                    await _delete(
                        conn, counts, "security_incidents",
                        "DELETE FROM security_incidents WHERE chat_id = $1",
                        chat_id,
                    )
                await _delete(
                    conn, counts, "messages",
                    "DELETE FROM messages WHERE chat_id = $1",
                    chat_id,
                )

                remaining = sum(
                    (
                        await conn.fetchval(
                            "SELECT count(*) FROM messages WHERE chat_id = $1",
                            chat_id,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM token_usage "
                            "WHERE chat_id = $1 OR user_id::text = ANY($2::text[])",
                            chat_id,
                            sorted(user_ids),
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM message_inbox "
                            "WHERE channel = $1 AND chat_id = $2",
                            DELETION_CHANNEL,
                            chat,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM outbound_messages "
                            "WHERE id::text = ANY($1::text[])",
                            outbound_ids,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM task_outbox "
                            "WHERE payload->>'chat_id' = $1 "
                            "OR payload->>'user_id' = ANY($2::text[]) "
                            "OR payload->>'customer_id' = $1 "
                            "OR payload->>'outbound_id' = ANY($3::text[]) "
                            "OR EXISTS (SELECT 1 FROM jsonb_array_elements_text("
                            "COALESCE(payload->'update_ids', '[]'::jsonb)) item(value) "
                            "WHERE item.value = ANY($4::text[]))",
                            chat,
                            sorted(user_ids),
                            outbound_ids,
                            update_ids,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM processing_consents "
                            "WHERE channel = $1 AND user_id = ANY($2::text[])",
                            DELETION_CHANNEL,
                            sorted(user_ids),
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM booking_events "
                            "WHERE scenario_id = ANY($1::uuid[])",
                            scenarios,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM scheduler_jobs "
                            "WHERE booking_key = ANY($1::uuid[]) "
                            "OR payload->>'customer_id' = $2",
                            booking_keys,
                            chat,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM booking_scenarios "
                            "WHERE customer_id = $1",
                            chat,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM bookings WHERE customer_id = $1",
                            chat,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM notification_feedback_requests "
                            "WHERE customer_id = $1",
                            chat,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM escalations WHERE customer_id = $1",
                            chat,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM human_mode WHERE customer_id = $1",
                            chat,
                        ),
                    )
                )
                if remaining:
                    raise CustomerDataDeletionError("customer data deletion failed")

                if await conn.fetchval(
                    "SELECT to_regclass('public.security_incidents')"
                ) and await conn.fetchval(
                    "SELECT count(*) FROM security_incidents WHERE chat_id = $1",
                    chat_id,
                ):
                    raise CustomerDataDeletionError("customer data deletion failed")

                status = "deleted" if sum(counts.values()) else "already_absent"
                await conn.execute(
                    """
                    INSERT INTO admin_audit_events (
                        actor_id, action, object_type, object_id,
                        before, after, ip_address, user_agent
                    ) VALUES ($1, 'customer_data.delete', 'customer_data', NULL,
                              NULL, $2::jsonb, $3, $4)
                    """,
                    actor_id,
                    json.dumps(
                        {
                            "channel": DELETION_CHANNEL,
                            "status": status,
                            "deleted_counts": counts,
                        },
                        ensure_ascii=False,
                    ),
                    ip_address,
                    user_agent,
                )
        return DeletionResult(status=status, deleted_counts=counts)
    except CustomerDataDeletionError:
        raise
    except Exception as error:
        raise CustomerDataDeletionError("customer data deletion failed") from error
    finally:
        if marker_set:
            try:
                await _delete_owned_marker(
                    redis_client,
                    marker,
                    marker_token,
                )
            except Exception:
                pass
        if buffer_lock_acquired and buffer_lock is not None:
            try:
                await buffer_lock.release()
            except Exception:
                pass
        if privacy_lock_acquired and privacy_lock is not None:
            try:
                await privacy_lock.release()
            except Exception:
                pass


async def delete_customer_data(
    *,
    pool,
    redis_client,
    chat_id: int,
    actor_id: int,
    ip_address: str | None,
    user_agent: str | None,
) -> DeletionResult:
    try:
        async with asyncio.timeout(DELETION_OPERATION_TIMEOUT_SECONDS):
            return await _delete_customer_data(
                pool=pool,
                redis_client=redis_client,
                chat_id=chat_id,
                actor_id=actor_id,
                ip_address=ip_address,
                user_agent=user_agent,
            )
    except TimeoutError as error:
        raise CustomerDataDeletionError(
            "customer data deletion failed"
        ) from error
