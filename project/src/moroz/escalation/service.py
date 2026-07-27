import json
from uuid import UUID, uuid4

from moroz.common.db import Database


class EscalationService:
    def __init__(self, database: Database):
        self._database = database

    async def create_low_rating(
        self,
        *,
        customer_id: str,
        booking_key: UUID,
        rating: int,
    ) -> UUID:
        escalation_id = uuid4()
        payload = json.dumps({"rating": rating}, ensure_ascii=False)
        async with self._database.acquire() as connection:
            async with connection.transaction():
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

