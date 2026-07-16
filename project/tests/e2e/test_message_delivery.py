import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import redis.asyncio as redis
from redis.exceptions import RedisError

from moroz.common.db import Database
from moroz.common.queue import QueueTask
from moroz.messaging.buffer import MessageBuffer
from moroz.messaging.models import IncomingMessage
from moroz.messaging.outbox import OutboxRelay, enqueue_process_message
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import DeliveryResult, TelegramSender
from worker.main import MessageTaskHandler, PipelinePump


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


class FakeSession:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakeTelegram:
    def __init__(self, error=None):
        self.error = error
        self.sent_messages = []
        self.session = FakeSession()

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=701)


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def __call__(self, text, context):
        self.calls.append((text, context))
        return SimpleNamespace(
            text="Готовый ответ",
            prompt_tokens=11,
            completion_tokens=7,
            cached_tokens=2,
            total_tokens=18,
            model="fake-model",
        )


class RecordingQueue:
    def __init__(self):
        self.tasks = []

    async def publish(self, task):
        self.tasks.append(task)


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=5)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def redis_client():
    client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


def incoming(update_id="100", text="Новый вопрос", chat_id="42"):
    return IncomingMessage(
        update_id=update_id,
        message_id="10",
        channel="telegram",
        chat_id=chat_id,
        user_id="7",
        text=text,
        received_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        correlation_id=uuid4(),
    )


async def test_worker_does_not_send_sent_outbound_twice(database):
    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Ответ",
        idempotency_key="reply:inbox-1",
    )
    telegram = FakeTelegram()
    sender = TelegramSender(telegram, repository)

    assert await sender.send(outbound_id) == DeliveryResult.SENT
    assert await sender.send(outbound_id) == DeliveryResult.SKIPPED

    assert telegram.sent_messages == [{"chat_id": 42, "text": "Ответ"}]
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT status FROM outbound_messages WHERE id = $1", outbound_id
        ) == "sent"


async def test_unknown_send_result_is_terminal_and_safe(
    database, caplog
):
    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Секретный ответ",
        idempotency_key="reply:unknown",
    )
    telegram = FakeTelegram(RuntimeError("sensitive failure detail"))
    sender = TelegramSender(telegram, repository)

    assert await sender.send(outbound_id) == DeliveryResult.DELIVERY_UNKNOWN
    assert await sender.send(outbound_id) == DeliveryResult.SKIPPED

    assert len(telegram.sent_messages) == 1
    assert "Секретный ответ" not in caplog.text
    assert "sensitive failure detail" not in caplog.text
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT status FROM outbound_messages WHERE id = $1", outbound_id
        ) == "delivery_unknown"
        assert await connection.fetchval(
            "SELECT count(*) FROM task_outbox WHERE kind = 'send_outbound'"
        ) == 1


async def test_cancelled_send_is_marked_unknown_before_cancellation_propagates(
    database,
):
    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Ответ при shutdown",
        idempotency_key="reply:cancelled",
    )
    sender = TelegramSender(FakeTelegram(asyncio.CancelledError()), repository)

    with pytest.raises(asyncio.CancelledError):
        await sender.send(outbound_id)

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT status FROM outbound_messages WHERE id = $1", outbound_id
        ) == "delivery_unknown"


async def test_process_message_materializes_reply_and_history_once(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming())
    async with database.acquire() as connection:
        await connection.execute(
            "INSERT INTO messages (chat_id, user_id, role, content) "
            "VALUES (42, 7, 'user', 'Старый вопрос'), "
            "(42, 7, 'assistant', 'Старый ответ')"
        )
    llm = FakeLLM()
    handler = MessageTaskHandler(database, llm, TelegramSender(FakeTelegram(), repository))
    task = QueueTask(
        kind="process_message",
        payload={
            "chat_id": "42",
            "update_ids": ["100"],
            "text": "Новый вопрос",
        },
        idempotency_key="process_message:100",
    )

    await handler.handle(task)
    await handler.handle(task)

    assert llm.calls == [
        (
            "Новый вопрос",
            [
                {"role": "user", "content": "Старый вопрос"},
                {"role": "assistant", "content": "Старый ответ"},
            ],
        )
    ]
    async with database.acquire() as connection:
        messages = await connection.fetch(
            "SELECT role, content FROM messages WHERE chat_id = 42 ORDER BY id"
        )
        usage = await connection.fetchrow(
            "SELECT prompt_tokens, completion_tokens, cached_tokens, "
            "total_tokens, model FROM token_usage"
        )
        outbound = await connection.fetchrow(
            "SELECT text, idempotency_key, status FROM outbound_messages"
        )
        tasks = await connection.fetch(
            "SELECT kind, status FROM task_outbox ORDER BY created_at, id"
        )
    assert [tuple(row.values()) for row in messages] == [
        ("user", "Старый вопрос"),
        ("assistant", "Старый ответ"),
        ("user", "Новый вопрос"),
        ("assistant", "Готовый ответ"),
    ]
    assert tuple(usage.values()) == (11, 7, 2, 18, "fake-model")
    assert tuple(outbound.values()) == (
        "Готовый ответ",
        "reply:process_message:100",
        "pending",
    )
    assert [tuple(row.values()) for row in tasks] == [("send_outbound", "pending")]


async def test_same_chat_process_tasks_are_serialized_by_postgres(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("101", "Первый"))
    assert await repository.accept(incoming("102", "Второй"))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = []

    async def blocking_llm(text, context):
        calls.append(text)
        if text == "Первый":
            first_started.set()
            await release_first.wait()
        return SimpleNamespace(
            text=f"Ответ: {text}",
            prompt_tokens=1,
            completion_tokens=1,
            cached_tokens=0,
            total_tokens=2,
            model="fake",
        )

    handler = MessageTaskHandler(
        database,
        blocking_llm,
        TelegramSender(FakeTelegram(), repository),
    )
    first = asyncio.create_task(
        handler.handle(
            QueueTask(
                "process_message",
                {"chat_id": "42", "update_ids": ["101"], "text": "Первый"},
                "process_message:101",
            )
        )
    )
    await first_started.wait()
    second = asyncio.create_task(
        handler.handle(
            QueueTask(
                "process_message",
                {"chat_id": "42", "update_ids": ["102"], "text": "Второй"},
                "process_message:102",
            )
        )
    )
    await asyncio.sleep(0)

    assert calls == ["Первый"]
    release_first.set()
    await asyncio.gather(first, second)
    assert calls == ["Первый", "Второй"]


async def test_fresh_pump_flushes_existing_due_buffer_and_publishes_all_pending(
    database, redis_client
):
    clock = Clock()
    old_buffer = MessageBuffer(redis_client, database, clock=clock)
    await old_buffer.append("42", "103", "До рестарта")
    await enqueue_process_message(
        database,
        chat_id="7",
        update_ids=("already-durable",),
        text="Уже в БД",
    )
    clock.advance(5)
    queue = RecordingQueue()
    fresh_pump = PipelinePump(
        MessageBuffer(redis_client, database, clock=clock),
        OutboxRelay(database, queue),
    )

    await fresh_pump.run_once()

    assert {task.idempotency_key for task in queue.tasks} == {
        "process_message:103",
        "process_message:already-durable",
    }
    assert await redis_client.exists("buffer:42") == 0


async def test_pump_publishes_database_tasks_when_redis_scan_fails(database):
    await enqueue_process_message(
        database,
        chat_id="42",
        update_ids=("durable",),
        text="Уже сохранено",
    )
    queue = RecordingQueue()

    class BrokenBuffer:
        async def due_chat_ids(self):
            raise RedisError("redis unavailable")

    await PipelinePump(BrokenBuffer(), OutboxRelay(database, queue)).run_once()

    assert [task.idempotency_key for task in queue.tasks] == [
        "process_message:durable"
    ]


async def test_handler_rejects_unknown_task_without_logging_data(
    database, caplog
):
    private = "private-payload-value"
    handler = MessageTaskHandler(
        database,
        FakeLLM(),
        TelegramSender(FakeTelegram(), MessageRepository(database)),
    )

    with pytest.raises(NotImplementedError, match="Unsupported worker task"):
        await handler.handle(
            QueueTask("unexpected", {"data": private}, f"private:{private}")
        )

    assert private not in caplog.text
