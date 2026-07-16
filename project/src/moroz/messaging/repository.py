import json
from uuid import UUID, uuid4

from moroz.common.db import Database
from moroz.messaging.models import IncomingMessage, OutboundMessage
from moroz.messaging.outbox import enqueue_process_message


class MessageRepository:
    def __init__(self, database: Database):
        self._database = database

    async def accept(self, message: IncomingMessage) -> bool:
        """Persist an update after the caller has verified processing consent."""
        payload = json.dumps(
            {
                "update_id": message.update_id,
                "message_id": message.message_id,
                "channel": message.channel,
                "chat_id": message.chat_id,
                "user_id": message.user_id,
                "text": message.text,
                "received_at": message.received_at.isoformat(),
                "correlation_id": str(message.correlation_id),
            },
            ensure_ascii=False,
        )
        async with self._database.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO message_inbox
                    (id, channel, external_message_id, chat_id, payload,
                     correlation_id)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6)
                ON CONFLICT (channel, external_message_id) DO NOTHING
                RETURNING id
                """,
                uuid4(),
                message.channel,
                message.update_id,
                message.chat_id,
                payload,
                message.correlation_id,
            )
        return row is not None

    async def enqueue_outbound(
        self,
        *,
        channel: str,
        chat_id: str,
        text: str,
        idempotency_key: str,
        delivery_options: dict[str, object] | None = None,
    ) -> UUID:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                return await self.enqueue_outbound_in_transaction(
                    connection,
                    channel=channel,
                    chat_id=chat_id,
                    text=text,
                    idempotency_key=idempotency_key,
                    delivery_options=delivery_options,
                )

    async def enqueue_outbound_in_transaction(
        self,
        connection,
        *,
        channel: str,
        chat_id: str,
        text: str,
        idempotency_key: str,
        delivery_options: dict[str, object] | None = None,
    ) -> UUID:
        outbound_id = uuid4()
        row = await connection.fetchrow(
            """
            INSERT INTO outbound_messages
                (id, channel, chat_id, text, delivery_options, idempotency_key)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            outbound_id,
            channel,
            chat_id,
            text,
            json.dumps(delivery_options or {}, ensure_ascii=False),
            idempotency_key,
        )
        if row is None:
            return await connection.fetchval(
                "SELECT id FROM outbound_messages WHERE idempotency_key = $1",
                idempotency_key,
            )
        await connection.execute(
            """
            INSERT INTO task_outbox (id, kind, payload, idempotency_key)
            VALUES ($1, 'send_outbound', $2::jsonb, $3)
            """,
            uuid4(),
            json.dumps({"outbound_id": str(outbound_id)}),
            f"send_outbound:{outbound_id}",
        )
        return outbound_id

    async def claim_outbound_delivery(
        self,
        outbound_id: UUID,
    ) -> OutboundMessage | None:
        async with self._database.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE outbound_messages
                SET status = 'sending', claimed_at = now()
                WHERE id = $1 AND status = 'pending'
                RETURNING id, channel, chat_id, text, delivery_options,
                          idempotency_key
                """,
                outbound_id,
            )
        if row is None:
            return None
        options = row["delivery_options"]
        return OutboundMessage(
            id=row["id"],
            channel=row["channel"],
            chat_id=row["chat_id"],
            text=row["text"],
            delivery_options=(
                json.loads(options) if isinstance(options, str) else options
            ),
            idempotency_key=row["idempotency_key"],
        )

    async def mark_outbound_sent(
        self,
        outbound_id: UUID,
        external_message_id: str,
    ) -> None:
        async with self._database.acquire() as connection:
            await connection.execute(
                """
                UPDATE outbound_messages
                SET status = 'sent', external_message_id = $2
                WHERE id = $1 AND status = 'sending'
                """,
                outbound_id,
                external_message_id,
            )

    async def mark_outbound_delivery_unknown(
        self,
        outbound_id: UUID,
    ) -> None:
        async with self._database.acquire() as connection:
            await connection.execute(
                """
                UPDATE outbound_messages
                SET status = 'delivery_unknown'
                WHERE id = $1 AND status = 'sending'
                """,
                outbound_id,
            )

    async def reconcile_stale_outbound_deliveries(self) -> int:
        async with self._database.acquire() as connection:
            result = await connection.execute(
                """
                UPDATE outbound_messages
                SET status = 'delivery_unknown'
                WHERE status = 'sending'
                """
            )
        return int(result.rsplit(" ", 1)[-1])

    async def enqueue_stale_accepted_messages(
        self,
        *,
        older_than_seconds: float,
        limit: int = 100,
    ) -> int:
        if limit <= 0:
            return 0
        async with self._database.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT inbox.external_message_id, inbox.chat_id, inbox.payload
                FROM message_inbox AS inbox
                WHERE inbox.channel = 'telegram'
                  AND inbox.status = 'accepted'
                  AND inbox.created_at <= now() - make_interval(
                      secs => $1::double precision
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM task_outbox AS task
                      WHERE task.kind = 'process_message'
                        AND task.payload->'update_ids'
                            ? inbox.external_message_id
                  )
                ORDER BY inbox.ingress_sequence
                LIMIT $2
                """,
                older_than_seconds,
                limit,
            )
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if (
                not isinstance(payload, dict)
                or payload.get("update_id") != row["external_message_id"]
                or payload.get("chat_id") != row["chat_id"]
                or not isinstance(payload.get("text"), str)
            ):
                raise ValueError("stale accepted inbox payload is invalid")
            await enqueue_process_message(
                self._database,
                chat_id=payload["chat_id"],
                update_ids=(payload["update_id"],),
                text=payload["text"],
            )
        return len(rows)
