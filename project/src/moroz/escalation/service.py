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

