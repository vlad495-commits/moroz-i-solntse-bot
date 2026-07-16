import asyncio
import json
import logging
import os
import signal
from pathlib import Path
from uuid import UUID

from aiogram import Bot
import redis.asyncio as redis
from redis.exceptions import RedisError

from config import CONTEXT_MESSAGES_LIMIT
from llm import generate_response, init_llm
from moroz.common.config import database_url_from_env
from moroz.common.db import Database
from moroz.common.queue import QueueTask, RabbitQueue
from moroz.messaging.buffer import MessageBuffer
from moroz.messaging.outbox import OutboxRelay
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import TelegramSender


logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")
READINESS_PATH = Path("/tmp/worker-ready")
PUMP_INTERVAL_SECONDS = 0.5


async def handle(task: QueueTask) -> None:
    logger.error("No worker task handler is registered; task will be retried")
    raise NotImplementedError("No worker task handlers are registered")


class MessageTaskHandler:
    def __init__(self, database: Database, llm, telegram: TelegramSender):
        self._database = database
        self._llm = llm
        self._telegram = telegram
        self._repository = MessageRepository(database)

    async def handle(self, task: QueueTask) -> None:
        if task.kind == "process_message":
            await self._process_message(task)
            return
        if task.kind == "send_outbound":
            outbound_id = task.payload.get("outbound_id")
            if not isinstance(outbound_id, str):
                raise ValueError("send_outbound requires outbound_id")
            await self._telegram.send(UUID(outbound_id))
            return
        logger.error("Unsupported worker task kind")
        raise NotImplementedError("Unsupported worker task")

    async def _process_message(self, task: QueueTask) -> None:
        chat_id = task.payload.get("chat_id")
        update_ids = task.payload.get("update_ids")
        text = task.payload.get("text")
        if (
            not isinstance(chat_id, str)
            or not isinstance(update_ids, list)
            or not update_ids
            or any(not isinstance(value, str) for value in update_ids)
            or not isinstance(text, str)
        ):
            raise ValueError("invalid process_message payload")
        numeric_chat_id = int(chat_id)
        reply_key = f"reply:{task.idempotency_key}"

        async with self._database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    chat_id,
                )
                if await connection.fetchval(
                    "SELECT id FROM outbound_messages WHERE idempotency_key = $1",
                    reply_key,
                ):
                    return

                inbox_rows = await connection.fetch(
                    """
                    SELECT payload
                    FROM message_inbox
                    WHERE channel = 'telegram'
                      AND chat_id = $1
                      AND external_message_id = ANY($2::text[])
                    """,
                    chat_id,
                    update_ids,
                )
                if len(inbox_rows) != len(set(update_ids)):
                    raise ValueError("process_message inbox rows are missing")
                payload = inbox_rows[0]["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                user_id = int(payload["user_id"])

                rows = await connection.fetch(
                    """
                    SELECT role, content
                    FROM messages
                    WHERE chat_id = $1
                    ORDER BY created_at DESC, id DESC
                    LIMIT $2
                    """,
                    numeric_chat_id,
                    CONTEXT_MESSAGES_LIMIT,
                )
                context = [
                    {"role": row["role"], "content": row["content"]}
                    for row in reversed(rows)
                ]
                result = await self._llm(text, context)

                await connection.execute(
                    """
                    INSERT INTO messages (chat_id, user_id, role, content)
                    VALUES ($1, $2, 'user', $3),
                           ($1, $2, 'assistant', $4)
                    """,
                    numeric_chat_id,
                    user_id,
                    text,
                    result.text,
                )
                await connection.execute(
                    """
                    INSERT INTO token_usage
                        (chat_id, user_id, prompt_tokens, completion_tokens,
                         cached_tokens, total_tokens, model)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    numeric_chat_id,
                    user_id,
                    result.prompt_tokens,
                    result.completion_tokens,
                    result.cached_tokens,
                    result.total_tokens,
                    result.model,
                )
                await self._repository.enqueue_outbound_in_transaction(
                    connection,
                    channel="telegram",
                    chat_id=chat_id,
                    text=result.text,
                    idempotency_key=reply_key,
                )
                await connection.execute(
                    """
                    UPDATE message_inbox
                    SET status = 'processed'
                    WHERE channel = 'telegram'
                      AND external_message_id = ANY($1::text[])
                    """,
                    update_ids,
                )


class PipelinePump:
    def __init__(self, buffer: MessageBuffer, relay: OutboxRelay):
        self._buffer = buffer
        self._relay = relay

    async def run_once(self) -> int:
        try:
            for chat_id in await self._buffer.due_chat_ids():
                await self._buffer.flush(chat_id)
        except RedisError as error:
            logger.warning(
                "pipeline_buffer_unavailable error_type=%s",
                type(error).__name__,
            )
        return await self._relay.publish_pending()

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=PUMP_INTERVAL_SECONDS
                )
            except TimeoutError:
                pass


def _remove_readiness(path: Path) -> None:
    path.unlink(missing_ok=True)


def _publish_readiness(path: Path, active: bool) -> None:
    if active:
        path.write_text("ready", encoding="utf-8")
    else:
        _remove_readiness(path)


async def _supervise(
    queue: RabbitQueue,
    stop: asyncio.Event,
    readiness_path: Path = READINESS_PATH,
    *,
    handler=handle,
    pump: PipelinePump | None = None,
) -> None:
    _remove_readiness(readiness_path)
    consumer = asyncio.create_task(
        queue.consume(
            handler,
            readiness=lambda active: _publish_readiness(readiness_path, active),
        )
    )
    waiter = asyncio.create_task(stop.wait())
    pump_task = asyncio.create_task(pump.run(stop)) if pump else None
    try:
        watched = {consumer, waiter}
        if pump_task:
            watched.add(pump_task)
        done, _ = await asyncio.wait(
            watched,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if waiter in done:
            return
        if consumer in done:
            await consumer
            raise RuntimeError("Consumer stopped unexpectedly")
        if pump_task in done:
            await pump_task
            raise RuntimeError("Pipeline pump stopped unexpectedly")
    finally:
        stop.set()
        _remove_readiness(readiness_path)
        consumer.cancel()
        waiter.cancel()
        await asyncio.gather(consumer, waiter, return_exceptions=True)
        if pump_task:
            await asyncio.gather(pump_task, return_exceptions=True)
        await queue.close()


async def run() -> None:
    _remove_readiness(READINESS_PATH)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    database_url = os.environ["DATABASE_URL"] or database_url_from_env(
        os.environ, required=True
    )
    redis_url = os.environ["REDIS_URL"]
    telegram_token = os.environ["TELEGRAM_BOT_TOKEN"]
    queue = RabbitQueue(os.environ["RABBITMQ_URL"])
    database = Database(database_url, min_size=1, max_size=5)
    redis_client = redis.from_url(redis_url, decode_responses=True)
    telegram = Bot(token=telegram_token)
    try:
        await database.connect()
        await redis_client.ping()
        await queue.connect()
        init_llm()
        repository = MessageRepository(database)
        task_handler = MessageTaskHandler(
            database,
            generate_response,
            TelegramSender(telegram, repository),
        )
        pump = PipelinePump(
            MessageBuffer(redis_client, database),
            OutboxRelay(database, queue),
        )
        logger.info("Worker started")
        await _supervise(queue, stop, handler=task_handler.handle, pump=pump)
    finally:
        _remove_readiness(READINESS_PATH)
        await queue.close()
        await telegram.session.close()
        await redis_client.aclose()
        await database.close()
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
