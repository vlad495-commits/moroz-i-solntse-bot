import json
from collections.abc import Sequence
from uuid import uuid4

from moroz.common.db import Database
from moroz.common.queue import QueuePort, QueueTask


def process_message_key(update_ids: Sequence[str]) -> str:
    return f"process_message:{','.join(update_ids)}"


async def enqueue_process_message(
    database: Database,
    *,
    chat_id: str,
    update_ids: Sequence[str],
    text: str,
) -> str:
    idempotency_key = process_message_key(update_ids)
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "update_ids": list(update_ids),
            "text": text,
        },
        ensure_ascii=False,
    )
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO task_outbox (id, kind, payload, idempotency_key)
            VALUES ($1, 'process_message', $2::jsonb, $3)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            uuid4(),
            payload,
            idempotency_key,
        )
    return idempotency_key


class OutboxRelay:
    def __init__(self, database: Database, queue: QueuePort):
        self._database = database
        self._queue = queue

    async def publish_pending(self, limit: int = 100) -> int:
        published = 0
        for _ in range(max(0, limit)):
            async with self._database.acquire() as connection:
                async with connection.transaction():
                    row = await connection.fetchrow(
                        """
                        SELECT id, kind, payload, idempotency_key
                        FROM task_outbox
                        WHERE status = 'pending'
                        ORDER BY created_at, id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                        """
                    )
                    if row is None:
                        return published
                    payload = row["payload"]
                    await self._queue.publish(
                        QueueTask(
                            kind=row["kind"],
                            payload=(
                                json.loads(payload)
                                if isinstance(payload, str)
                                else payload
                            ),
                            idempotency_key=row["idempotency_key"],
                        )
                    )
                    await connection.execute(
                        """
                        UPDATE task_outbox
                        SET status = 'published', published_at = now()
                        WHERE id = $1
                        """,
                        row["id"],
                    )
            published += 1
        return published
