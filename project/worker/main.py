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
from llm import generate_response, init_llm, prompt_reload_listener
from moroz.common.config import database_url_from_env
from moroz.common.db import Database
from moroz.common.queue import QueueTask, RabbitQueue
from moroz.messaging.buffer import MessageBuffer
from moroz.messaging.outbox import OutboxRelay, process_message_key
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import TelegramSender


logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")
READINESS_PATH = Path("/tmp/worker-ready")
PUMP_INTERVAL_SECONDS = 0.5
PUMP_SHUTDOWN_TIMEOUT_SECONDS = 5.0
PROMPT_SHUTDOWN_TIMEOUT_SECONDS = 5.0


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
            or len(set(update_ids)) != len(update_ids)
            or not isinstance(text, str)
        ):
            raise ValueError("invalid process_message payload")
        if task.idempotency_key != process_message_key(update_ids):
            raise ValueError("process_message idempotency key does not match updates")
        numeric_chat_id = int(chat_id)
        reply_key = f"reply:{task.idempotency_key}"

        async with self._database.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    chat_id,
                )
                inbox_rows = await connection.fetch(
                    """
                    SELECT external_message_id, payload, status, ingress_sequence
                    FROM message_inbox
                    WHERE channel = 'telegram'
                      AND chat_id = $1
                      AND (
                          status = 'accepted'
                          OR external_message_id = ANY($2::text[])
                      )
                    ORDER BY ingress_sequence
                    FOR UPDATE
                    """,
                    chat_id,
                    update_ids,
                )
                requested = {
                    row["external_message_id"]: row
                    for row in inbox_rows
                    if row["external_message_id"] in update_ids
                }
                if len(requested) != len(update_ids):
                    raise ValueError("process_message inbox rows are missing")
                if [
                    row["external_message_id"]
                    for row in inbox_rows
                    if row["external_message_id"] in requested
                ] != update_ids:
                    raise ValueError(
                        "process_message update ids are outside ingress order"
                    )
                if any(
                    row["status"] not in {"accepted", "processed"}
                    for row in requested.values()
                ):
                    raise ValueError("process_message inbox status is invalid")

                accepted = [
                    row
                    for row in inbox_rows
                    if row["status"] == "accepted"
                    and row["external_message_id"] in requested
                ]
                if not accepted:
                    return
                all_accepted = [
                    row for row in inbox_rows if row["status"] == "accepted"
                ]
                if [
                    row["external_message_id"]
                    for row in all_accepted[: len(accepted)]
                ] != [row["external_message_id"] for row in accepted]:
                    raise ValueError(
                        "process_message has an earlier accepted inbox row"
                    )

                payloads = []
                for row in accepted:
                    payload = row["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    if (
                        not isinstance(payload, dict)
                        or payload.get("update_id") != row["external_message_id"]
                        or payload.get("chat_id") != chat_id
                        or not isinstance(payload.get("text"), str)
                        or not isinstance(payload.get("user_id"), str)
                    ):
                        raise ValueError("process_message persisted payload is invalid")
                    payloads.append(payload)
                user_ids = {payload["user_id"] for payload in payloads}
                if len(user_ids) != 1:
                    raise ValueError("process_message spans multiple users")
                user_id = int(user_ids.pop())
                persisted_text = "\n".join(payload["text"] for payload in payloads)

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
                result = await self._llm(persisted_text, context)

                await connection.execute(
                    """
                    INSERT INTO messages (chat_id, user_id, role, content)
                    VALUES ($1, $2, 'user', $3),
                           ($1, $2, 'assistant', $4)
                    """,
                    numeric_chat_id,
                    user_id,
                    persisted_text,
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
                    [row["external_message_id"] for row in accepted],
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


def _consume_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _stop_background_task(task: asyncio.Task, timeout: float) -> None:
    await asyncio.sleep(0)
    if not task.done():
        task.cancel()
    done, _ = await asyncio.wait((task,), timeout=timeout)
    if done:
        _consume_task_result(task)
    else:
        task.add_done_callback(_consume_task_result)


async def _supervise(
    queue: RabbitQueue,
    stop: asyncio.Event,
    readiness_path: Path = READINESS_PATH,
    *,
    handler=handle,
    pump: PipelinePump | None = None,
    prompt_listener=None,
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
    prompt_task = (
        asyncio.create_task(prompt_listener()) if prompt_listener else None
    )
    try:
        watched = {consumer, waiter}
        if pump_task:
            watched.add(pump_task)
        if prompt_task:
            watched.add(prompt_task)
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
        if prompt_task in done:
            await prompt_task
            raise RuntimeError("Prompt reload listener stopped unexpectedly")
    finally:
        stop.set()
        _remove_readiness(readiness_path)
        consumer.cancel()
        waiter.cancel()
        await asyncio.gather(consumer, waiter, return_exceptions=True)
        if pump_task:
            await _stop_background_task(
                pump_task, PUMP_SHUTDOWN_TIMEOUT_SECONDS
            )
        if prompt_task:
            await _stop_background_task(
                prompt_task, PROMPT_SHUTDOWN_TIMEOUT_SECONDS
            )
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
        repository = MessageRepository(database)
        reconciled = await repository.reconcile_stale_outbound_deliveries()
        if reconciled:
            logger.warning(
                "stale_outbound_deliveries_terminalized count=%d", reconciled
            )
        await redis_client.ping()
        await queue.connect()
        init_llm()
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
        await _supervise(
            queue,
            stop,
            handler=task_handler.handle,
            pump=pump,
            prompt_listener=prompt_reload_listener,
        )
    finally:
        _remove_readiness(READINESS_PATH)
        await queue.close()
        await telegram.session.close()
        await redis_client.aclose()
        await database.close()
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
