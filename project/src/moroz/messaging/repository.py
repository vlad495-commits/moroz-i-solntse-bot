import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Awaitable, Callable, Literal
from uuid import UUID, uuid4

import asyncpg

from moroz.common.db import Database
from moroz.messaging.models import IncomingMessage, OutboundMessage
from moroz.messaging.outbox import (
    enqueue_process_message,
    enqueue_process_message_in_transaction,
)
from moroz.escalation.service import (
    ADMIN_REPLY_PREFIX,
    complete_admin_reply_delivery,
    parse_admin_reply_key,
)
from moroz.privacy import customer_lock_subject


PreSendGuard = Callable[
    [asyncpg.Connection, OutboundMessage], Awaitable[bool]
]
TerminalTransition = Callable[[], Awaitable[OutboundMessage | None]]
DeliveryHook = Callable[
    [
        asyncpg.Connection,
        OutboundMessage,
        Literal["sent", "failed", "delivery_unknown"],
        str | None,
        datetime,
        TerminalTransition,
    ],
    Awaitable[None],
]


class OutboundDeliveryBlocked(Exception):
    """An earlier delivery for the same chat is still non-terminal."""


def _outbound_from_row(row) -> OutboundMessage:
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


class MessageRepository:
    def __init__(self, database: Database):
        self._database = database

    async def accept(self, message: IncomingMessage) -> bool:
        """Persist an update after the caller has verified processing consent."""
        async with self._database.acquire() as connection:
            return await self._insert_incoming(connection, message)

    async def accept_if_consented(
        self,
        message: IncomingMessage,
        *,
        enqueue_directly: bool = False,
    ) -> bool:
        """Serialize privacy-sensitive ingress and recheck durable consent."""
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    customer_lock_subject(message.chat_id),
                )
                consented = await connection.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM processing_consents
                        WHERE channel = $1 AND user_id = $2
                    )
                    """,
                    message.channel,
                    message.user_id,
                )
                if not consented:
                    return False
                accepted = await self._insert_incoming(connection, message)
                if accepted and enqueue_directly:
                    await enqueue_process_message_in_transaction(
                        connection,
                        update_ids=(message.update_id,),
                    )
                return accepted

    async def _insert_incoming(self, connection, message: IncomingMessage) -> bool:
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
                "kind": message.kind,
                "data": dict(message.data),
            },
            ensure_ascii=False,
        )
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
            async with connection.transaction():
                target = await connection.fetchrow(
                    """
                    SELECT channel, chat_id, status, created_at
                    FROM outbound_messages
                    WHERE id = $1
                    """,
                    outbound_id,
                )
                if target is None or target["status"] != "pending":
                    return None
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f'{target["channel"]}:{target["chat_id"]}',
                )
                row = await connection.fetchrow(
                    """
                    UPDATE outbound_messages AS target
                    SET status = 'sending', claimed_at = now()
                    WHERE target.id = $1
                      AND target.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM outbound_messages AS earlier
                          WHERE earlier.channel = target.channel
                            AND earlier.chat_id = target.chat_id
                            AND earlier.status IN ('pending', 'sending')
                            AND (
                                earlier.created_at < target.created_at
                                OR (
                                    earlier.created_at = target.created_at
                                    AND earlier.id < target.id
                                )
                            )
                      )
                    RETURNING target.id, target.channel, target.chat_id,
                              target.text, target.delivery_options,
                              target.idempotency_key
                    """,
                    outbound_id,
                )
                if row is None:
                    status = await connection.fetchval(
                        "SELECT status FROM outbound_messages WHERE id = $1",
                        outbound_id,
                    )
                    if status == "pending":
                        raise OutboundDeliveryBlocked
        if row is None:
            return None
        return _outbound_from_row(row)

    async def get_sending_outbound(
        self, outbound_id: UUID
    ) -> OutboundMessage | None:
        async with self._database.acquire() as connection:
            return await self._sending_outbound_snapshot(connection, outbound_id)

    async def get_outbound_delivery_status(self, outbound_id: UUID) -> str | None:
        async with self._database.acquire() as connection:
            return await connection.fetchval(
                "SELECT status FROM outbound_messages WHERE id = $1", outbound_id
            )

    @asynccontextmanager
    async def fence_claimed_outbound(
        self,
        outbound: OutboundMessage,
        *,
        pre_send_guard: PreSendGuard | None = None,
    ):
        async with self._database.acquire() as connection:
            async with connection.transaction():
                if pre_send_guard is not None and not await pre_send_guard(
                    connection, outbound
                ):
                    yield None
                    return
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    customer_lock_subject(outbound.chat_id),
                )
                row = await connection.fetchrow(
                    """
                    SELECT id, channel, chat_id, text, delivery_options,
                           idempotency_key
                    FROM outbound_messages
                    WHERE id = $1 AND channel = $2 AND chat_id = $3
                      AND status = 'sending'
                    """,
                    outbound.id,
                    outbound.channel,
                    outbound.chat_id,
                )
                yield None if row is None else _outbound_from_row(row)

    async def mark_outbound_sent(
        self,
        outbound_id: UUID,
        external_message_id: str,
        *,
        delivery_hook: DeliveryHook | None = None,
        now: datetime | None = None,
    ) -> str | None:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                outbound = await self._sending_outbound_snapshot(
                    connection, outbound_id
                )
                if outbound is None:
                    return None
                outbound = await self._complete_terminal_outbound(
                    connection,
                    outbound,
                    "sent",
                    external_message_id=external_message_id,
                    delivery_hook=delivery_hook,
                    error_code=None,
                    now=now,
                )
                parsed = parse_admin_reply_key(outbound.idempotency_key)
                if parsed is None:
                    if outbound.idempotency_key.startswith(
                        f"{ADMIN_REPLY_PREFIX}:"
                    ):
                        raise ValueError("malformed admin reply key")
                    return None
                escalation_id, _ = parsed
                if outbound.channel != "telegram":
                    raise ValueError("admin reply channel mismatch")
                await complete_admin_reply_delivery(
                    connection,
                    outbound_id=outbound_id,
                    escalation_id=escalation_id,
                    chat_id=outbound.chat_id,
                    text=outbound.text,
                )
                return outbound.chat_id

    async def mark_outbound_failed(
        self,
        outbound_id: UUID,
        error_code: str,
        *,
        delivery_hook: DeliveryHook | None = None,
        now: datetime | None = None,
    ) -> bool:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                outbound = await self._sending_outbound_snapshot(
                    connection, outbound_id
                )
                if outbound is None:
                    return False
                await self._complete_terminal_outbound(
                    connection,
                    outbound,
                    "failed",
                    delivery_hook=delivery_hook,
                    error_code=error_code,
                    now=now,
                )
                return True

    async def mark_outbound_delivery_unknown(
        self,
        outbound_id: UUID,
        *,
        delivery_hook: DeliveryHook | None = None,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                outbound = await self._sending_outbound_snapshot(
                    connection, outbound_id
                )
                if outbound is None:
                    if delivery_hook is not None:
                        outbound = await self._outbound_status_snapshot(
                            connection, outbound_id, "delivery_unknown"
                        )
                if outbound is None:
                    return bool(
                        await connection.fetchval(
                            "SELECT status = 'delivery_unknown' "
                            "FROM outbound_messages WHERE id = $1",
                            outbound_id,
                        )
                    )
                await self._complete_terminal_outbound(
                    connection,
                    outbound,
                    "delivery_unknown",
                    delivery_hook=delivery_hook,
                    error_code=error_code,
                    now=now,
                    allow_existing_terminal=True,
                )
                return True

    async def _complete_terminal_outbound(
        self,
        connection,
        outbound: OutboundMessage,
        status: Literal["sent", "failed", "delivery_unknown"],
        *,
        external_message_id: str | None = None,
        delivery_hook: DeliveryHook | None,
        error_code: str | None,
        now: datetime | None,
        allow_existing_terminal: bool = False,
    ) -> OutboundMessage:
        transition_calls = 0
        transitioned = None

        async def transition():
            nonlocal transition_calls, transitioned
            transition_calls += 1
            if transition_calls != 1:
                raise RuntimeError("terminal transition must be called exactly once")
            transitioned = await self._transition_sending_outbound(
                connection, outbound.id, status, external_message_id
            )
            if transitioned is None and allow_existing_terminal:
                transitioned = await self._outbound_status_snapshot(
                    connection, outbound.id, status
                )
            if transitioned is None:
                raise RuntimeError("terminal transition was not persisted")
            return transitioned

        if delivery_hook is not None:
            if now is None:
                raise ValueError("delivery hook timestamp is required")
            await delivery_hook(
                connection, outbound, status, error_code, now, transition
            )
        else:
            await transition()
        if transition_calls != 1 or transitioned is None:
            raise RuntimeError("terminal transition must be called exactly once")
        durable_status = await connection.fetchval(
            "SELECT status FROM outbound_messages WHERE id = $1", outbound.id
        )
        if durable_status != status:
            raise RuntimeError("terminal transition status was not persisted")
        return transitioned

    @staticmethod
    async def _sending_outbound_snapshot(connection, outbound_id: UUID):
        return await MessageRepository._outbound_status_snapshot(
            connection, outbound_id, "sending"
        )

    @staticmethod
    async def _outbound_status_snapshot(connection, outbound_id: UUID, status: str):
        row = await connection.fetchrow(
            """
            SELECT id, channel, chat_id, text, delivery_options,
                   idempotency_key
            FROM outbound_messages
            WHERE id = $1 AND status = $2
            """,
            outbound_id,
            status,
        )
        return None if row is None else _outbound_from_row(row)

    @staticmethod
    async def _transition_sending_outbound(
        connection,
        outbound_id: UUID,
        status: Literal["sent", "failed", "delivery_unknown"],
        external_message_id: str | None = None,
    ):
        row = await connection.fetchrow(
            """
            UPDATE outbound_messages
            SET status = $2,
                external_message_id = COALESCE($3, external_message_id)
            WHERE id = $1 AND status = 'sending'
            RETURNING id, channel, chat_id, text, delivery_options,
                      idempotency_key
            """,
            outbound_id,
            status,
            external_message_id,
        )
        return None if row is None else _outbound_from_row(row)

    async def release_outbound_delivery(self, outbound_id: UUID) -> bool:
        async with self._database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    UPDATE outbound_messages
                    SET status = 'pending', claimed_at = NULL
                    WHERE id = $1 AND status = 'sending'
                    """,
                    outbound_id,
                )
                return bool(
                    await connection.fetchval(
                        "SELECT status = 'pending' FROM outbound_messages "
                        "WHERE id = $1",
                        outbound_id,
                    )
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
            )
        return len(rows)
