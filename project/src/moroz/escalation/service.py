import json
from uuid import UUID, uuid4

from moroz.common.db import Database
from moroz.privacy import customer_lock_subject


ADMIN_REPLY_PREFIX = "admin_handoff_reply"


def admin_reply_key(escalation_id: UUID, reply_token: UUID) -> str:
    return f"{ADMIN_REPLY_PREFIX}:{escalation_id}:{reply_token}"


def parse_admin_reply_key(value: str) -> tuple[UUID, UUID] | None:
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != ADMIN_REPLY_PREFIX:
        return None
    try:
        return UUID(parts[1]), UUID(parts[2])
    except ValueError:
        return None


async def complete_admin_reply_delivery(
    connection,
    *,
    outbound_id: UUID,
    escalation_id: UUID,
    chat_id: str,
    text: str,
) -> None:
    """Materialize a confirmed staff reply inside the delivery transaction."""
    escalation = await connection.fetchrow(
        """
        SELECT customer_id, status
        FROM escalations
        WHERE id = $1
        FOR UPDATE
        """,
        escalation_id,
    )
    if (
        escalation is None
        or escalation["status"] != "open"
        or escalation["customer_id"] != chat_id
    ):
        raise ValueError("admin reply escalation mismatch")
    mode = await connection.fetchrow(
        """
        SELECT enabled
        FROM human_mode
        WHERE customer_id = $1
        FOR UPDATE
        """,
        chat_id,
    )
    if mode is None or not mode["enabled"]:
        raise ValueError("admin reply human mode is inactive")
    queued_audit = await connection.fetchrow(
        """
        SELECT actor_id, ip_address, user_agent
        FROM admin_audit_events
        WHERE action = 'escalation.reply_queued'
          AND object_type = 'escalation'
          AND object_id = $1
          AND after->>'outbound_id' = $2
        ORDER BY id DESC
        LIMIT 1
        """,
        str(escalation_id),
        str(outbound_id),
    )
    if queued_audit is None:
        raise ValueError("admin reply queued audit is missing")
    numeric_chat_id = int(chat_id)
    identity = await connection.fetchrow(
        """
        SELECT user_id, username
        FROM messages
        WHERE chat_id = $1::bigint AND user_id IS NOT NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        numeric_chat_id,
    )
    await connection.execute(
        """
        INSERT INTO messages (chat_id, user_id, username, role, content)
        VALUES ($1::bigint, $2, $3, 'assistant', $4)
        """,
        numeric_chat_id,
        identity["user_id"] if identity else None,
        identity["username"] if identity else None,
        text,
    )
    await connection.execute(
        """
        UPDATE escalations
        SET status = 'resolved', resolved_at = now()
        WHERE id = $1
        """,
        escalation_id,
    )
    has_open = await connection.fetchval(
        """
        SELECT EXISTS(
            SELECT 1 FROM escalations
            WHERE customer_id = $1 AND status = 'open'
        )
        """,
        chat_id,
    )
    if not has_open:
        await connection.execute(
            """
            UPDATE human_mode
            SET enabled = false, expires_at = now() + interval '5 minutes'
            WHERE customer_id = $1
            """,
            chat_id,
        )
    await connection.execute(
        """
        INSERT INTO admin_audit_events (
            actor_id, action, object_type, object_id,
            before, after, ip_address, user_agent
        )
        VALUES (
            $1, 'escalation.reply_delivered', 'escalation', $2,
            '{"status":"queued"}'::jsonb,
            '{"status":"delivered"}'::jsonb,
            $3, $4
        )
        """,
        queued_audit["actor_id"],
        str(escalation_id),
        queued_audit["ip_address"],
        queued_audit["user_agent"],
    )


class EscalationService:
    def __init__(self, database: Database):
        self._database = database

    async def create_low_rating(
        self,
        *,
        customer_id: str,
        booking_key: UUID,
        rating: int,
    ) -> UUID | None:
        escalation_id = uuid4()
        payload = json.dumps({"rating": rating}, ensure_ascii=False)
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    customer_lock_subject(customer_id),
                )
                cooldown_active = await connection.fetchval(
                    """
                    SELECT COALESCE(
                        enabled = false AND expires_at > now(),
                        false
                    )
                    FROM human_mode
                    WHERE customer_id = $1
                    FOR UPDATE
                    """,
                    customer_id,
                )
                if cooldown_active:
                    return None
                await connection.execute(
                    """
                    INSERT INTO escalations
                        (id, source, customer_id, booking_key, status,
                         reason_code, payload)
                    VALUES ($1, 'feedback', $2, $3, 'open',
                            'low_feedback_rating', $4::jsonb)
                    """,
                    escalation_id,
                    customer_id,
                    booking_key,
                    payload,
                )
                await connection.execute(
                    """
                    INSERT INTO human_mode
                        (customer_id, enabled, reason_code, escalation_id,
                         enabled_at)
                    VALUES ($1, true, 'low_feedback_rating', $2, now())
                    ON CONFLICT (customer_id) DO UPDATE SET
                        enabled = true,
                        reason_code = EXCLUDED.reason_code,
                        escalation_id = EXCLUDED.escalation_id,
                        enabled_at = EXCLUDED.enabled_at,
                        expires_at = NULL
                    """,
                    customer_id,
                    escalation_id,
                )
        return escalation_id

