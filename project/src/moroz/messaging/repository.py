import json
from uuid import UUID, uuid4

from moroz.common.db import Database
from moroz.messaging.models import IncomingMessage, OutboundMessage


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
        outbound_id = uuid4()
        async with self._database.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    INSERT INTO outbound_messages
                        (id, channel, chat_id, text, delivery_options,
                         idempotency_key)
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
                    INSERT INTO task_outbox
                        (id, kind, payload, idempotency_key)
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
                SET status = 'sending'
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
