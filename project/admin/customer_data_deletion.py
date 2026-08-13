"""Owner-triggered removal of one Telegram customer's local data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from moroz.privacy import DELETION_MARKER_TTL_SECONDS, deletion_marker_key


DELETION_CHANNEL = "telegram"


class CustomerDataDeletionError(RuntimeError):
    """Safe public failure without customer or infrastructure details."""


@dataclass(frozen=True)
class DeletionResult:
    status: Literal["deleted", "already_absent"]
    deleted_counts: dict[str, int]


def _delete_count(command_tag: str) -> int:
    try:
        return int(command_tag.rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


async def _clear_redis(redis_client, chat_id: str, user_ids: set[str]) -> None:
    pipe = redis_client.pipeline(transaction=True)
    pipe.delete(
        f"chat:{chat_id}:messages",
        f"buffer:{chat_id}",
        f"lock:buffer:{chat_id}",
    )
    pipe.zrem("buffer:deadlines", chat_id)
    for user_id in sorted(user_ids):
        pipe.delete(f"consent:state:telegram:{chat_id}:{user_id}")
    await pipe.execute()


async def _delete(conn, counts: dict[str, int], name: str, query: str, *args) -> None:
    counts[name] = _delete_count(await conn.execute(query, *args))


async def delete_customer_data(
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
    marker_set = False
    try:
        await redis_client.set(
            marker,
            "1",
            ex=DELETION_MARKER_TTL_SECONDS,
        )
        marker_set = True
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"{DELETION_CHANNEL}:{chat}",
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
                        WHERE channel = $1 AND chat_id = $2
                        """,
                        DELETION_CHANNEL,
                        chat,
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
                    """,
                    chat,
                    sorted(user_ids),
                    outbound_ids,
                )
                await _delete(
                    conn, counts, "outbound_messages",
                    "DELETE FROM outbound_messages WHERE channel = $1 AND chat_id = $2",
                    DELETION_CHANNEL,
                    chat,
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
                            "WHERE channel = $1 AND chat_id = $2",
                            DELETION_CHANNEL,
                            chat,
                        ),
                        await conn.fetchval(
                            "SELECT count(*) FROM task_outbox "
                            "WHERE payload->>'chat_id' = $1 "
                            "OR payload->>'user_id' = ANY($2::text[]) "
                            "OR payload->>'customer_id' = $1 "
                            "OR payload->>'outbound_id' = ANY($3::text[])",
                            chat,
                            sorted(user_ids),
                            outbound_ids,
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
                await redis_client.delete(marker)
            except Exception:
                pass
