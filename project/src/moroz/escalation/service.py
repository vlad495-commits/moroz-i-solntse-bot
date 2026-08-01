import json
from hashlib import sha256
from uuid import UUID, uuid4

from moroz.common.db import Database
from moroz.messaging.repository import MessageRepository


class EscalationNotOpen(Exception):
    pass


class EscalationService:
    def __init__(self, database: Database):
        self._database = database
        self._messages = MessageRepository(database)

    async def list_open(self) -> list[dict[str, object]]:
        async with self._database.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT e.id, e.customer_id, e.reason_code, e.created_at,
                       e.payload->>'scenario_id' AS scenario_id,
                       COALESCE(
                           jsonb_agg(mi.payload->>'text' ORDER BY mi.created_at)
                           FILTER (
                               WHERE mi.id IS NOT NULL
                                 AND mi.payload->>'kind' = 'text'
                                 AND jsonb_typeof(mi.payload->'text') = 'string'
                           ),
                           '[]'::jsonb
                       ) AS messages
                FROM escalations AS e
                LEFT JOIN message_inbox AS mi
                  ON mi.channel = 'telegram'
                 AND mi.chat_id = e.customer_id
                 AND mi.created_at >= e.created_at
                WHERE e.status = 'open'
                GROUP BY e.id
                ORDER BY e.created_at, e.id
                """
            )
        result = []
        for row in rows:
            messages = row["messages"]
            if isinstance(messages, str):
                messages = json.loads(messages)
            result.append(
                {
                    "id": row["id"],
                    "customer_id": row["customer_id"],
                    "reason_code": row["reason_code"],
                    "scenario_id": row["scenario_id"],
                    "created_at": row["created_at"],
                    "messages": list(messages),
                }
            )
        return result

    async def reply(
        self,
        escalation_id: UUID,
        *,
        text: str,
        actor_id: int | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> bool:
        digest = sha256(text.encode("utf-8")).hexdigest()
        idempotency_key = f"escalation_reply:{escalation_id}:{digest}"
        async with self._database.acquire() as connection:
            async with connection.transaction():
                escalation = await connection.fetchrow(
                    """
                    SELECT customer_id, status
                    FROM escalations
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    escalation_id,
                )
                if escalation is None or escalation["status"] != "open":
                    raise EscalationNotOpen
                exists = await connection.fetchval(
                    """SELECT EXISTS (
                           SELECT 1 FROM outbound_messages
                           WHERE idempotency_key = $1
                       )""",
                    idempotency_key,
                )
                if exists:
                    return False
                await self._messages.enqueue_outbound_in_transaction(
                    connection,
                    channel="telegram",
                    chat_id=escalation["customer_id"],
                    text=text,
                    idempotency_key=idempotency_key,
                )
                await self._record_audit(
                    connection,
                    actor_id=actor_id,
                    action="escalation.reply",
                    escalation_id=escalation_id,
                    before={"status": "open"},
                    after={"status": "open"},
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
        return True

    async def resolve(
        self,
        escalation_id: UUID,
        *,
        reason: str,
        actor_id: int | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                escalation = await connection.fetchrow(
                    """
                    SELECT customer_id, status
                    FROM escalations
                    WHERE id = $1
                    FOR UPDATE
                    """,
                    escalation_id,
                )
                if escalation is None or escalation["status"] != "open":
                    raise EscalationNotOpen
                await connection.execute(
                    """
                    UPDATE escalations
                    SET status = 'resolved', resolved_at = now(),
                        resolved_by = $2, resolution_reason = $3
                    WHERE id = $1 AND status = 'open'
                    """,
                    escalation_id,
                    str(actor_id) if actor_id is not None else None,
                    reason,
                )
                await connection.execute(
                    """
                    UPDATE human_mode
                    SET enabled = false
                    WHERE escalation_id = $1 AND customer_id = $2
                    """,
                    escalation_id,
                    escalation["customer_id"],
                )
                await self._record_audit(
                    connection,
                    actor_id=actor_id,
                    action="escalation.resolve",
                    escalation_id=escalation_id,
                    before={"status": "open"},
                    after={"status": "resolved", "reason": reason},
                    ip_address=ip_address,
                    user_agent=user_agent,
                )

    @staticmethod
    async def _record_audit(
        connection,
        *,
        actor_id: int | None,
        action: str,
        escalation_id: UUID,
        before: dict[str, object],
        after: dict[str, object],
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        await connection.execute(
            """
            INSERT INTO admin_audit_events
                (actor_id, action, object_type, object_id,
                 before, after, ip_address, user_agent)
            VALUES ($1, $2, 'escalation', $3, $4::jsonb, $5::jsonb, $6, $7)
            """,
            actor_id,
            action,
            str(escalation_id),
            json.dumps(before, ensure_ascii=False),
            json.dumps(after, ensure_ascii=False),
            ip_address,
            user_agent,
        )

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

