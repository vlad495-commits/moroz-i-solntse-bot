import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
import redis.asyncio as redis

from moroz.common.db import Database
from moroz.messaging.buffer import BufferedMessage, MessageBuffer
from moroz.messaging.models import IncomingMessage
from moroz.messaging.outbox import enqueue_process_message
from moroz.messaging.repository import MessageRepository
from moroz.messaging.service import MessageService


pytestmark = pytest.mark.asyncio


@dataclass
class Clock:
    value: datetime = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)

    def now(self):
        return self.value

    def advance(self, *, seconds):
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


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def buffer(redis_client, database, clock):
    return MessageBuffer(redis_client, database, clock=clock)


def incoming(update_id, text="Текст", *, chat_id="42"):
    return IncomingMessage(
        update_id=str(update_id),
        message_id="100",
        channel="telegram",
        chat_id=chat_id,
        user_id="7",
        text=text,
        received_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        correlation_id=uuid4(),
    )


async def test_buffer_joins_fast_messages_after_deadline(
    buffer, clock, database
):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("1", "Хочу"))
    assert await repository.accept(incoming("2", "на крио"))

    await buffer.append("42", "1", "Хочу")
    clock.advance(seconds=2)
    await buffer.append("42", "2", "на крио")

    assert await buffer.flush("42") is None
    clock.advance(seconds=5)
    flushed = await buffer.flush("42")

    async with database.acquire() as connection:
        task = await connection.fetchrow(
            "SELECT kind, payload, idempotency_key, status FROM task_outbox"
        )
    assert flushed == BufferedMessage(
        chat_id="42",
        update_ids=("1", "2"),
        text="Хочу\nна крио",
    )
    assert task["kind"] == "process_message"
    assert json.loads(task["payload"]) == {
        "chat_id": "42",
        "update_ids": ["1", "2"],
    }
    assert task["idempotency_key"] == "process_message:1,2"
    assert task["status"] == "pending"


async def test_concurrent_flush_claims_batch_once(buffer, clock, database):
    await buffer.append("42", "3", "Один раз")
    clock.advance(seconds=5)

    first, second = await asyncio.gather(
        buffer.flush("42"),
        buffer.flush("42"),
    )

    assert sorted(
        result is not None for result in (first, second)
    ) == [False, True]
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM task_outbox"
        ) == 1


async def test_flush_reuses_committed_task_after_redis_delete_gap(
    buffer, clock, database, redis_client
):
    await buffer.append("42", "4", "Сохранено")
    buffered = BufferedMessage("42", ("4",), "Сохранено")
    committed_key = await enqueue_process_message(
        database,
        chat_id=buffered.chat_id,
        update_ids=buffered.update_ids,
    )
    clock.advance(seconds=5)

    flushed = await buffer.flush("42")

    async with database.acquire() as connection:
        rows = await connection.fetch(
            "SELECT idempotency_key FROM task_outbox"
        )
    assert flushed == buffered
    assert [row["idempotency_key"] for row in rows] == [committed_key]
    assert await redis_client.zscore("buffer:deadlines", "42") is None


async def test_due_chat_discovery_has_hard_limit_and_ignores_future(
    redis_client, database, clock
):
    now = clock.now().timestamp()
    await redis_client.zadd(
        "buffer:deadlines",
        {
            **{f"due-{index:03d}": now - 1 for index in range(250)},
            **{f"future-{index:03d}": now + 60 for index in range(250)},
        },
    )

    due = await MessageBuffer(
        redis_client, database, clock=clock
    ).due_chat_ids(limit=7)

    assert len(due) == 7
    assert all(chat_id.startswith("due-") for chat_id in due)


async def test_flush_removes_due_orphan_from_deadline_index(
    redis_client, database, clock
):
    await redis_client.zadd(
        "buffer:deadlines", {"orphan": clock.now().timestamp() - 1}
    )
    buffer = MessageBuffer(redis_client, database, clock=clock)

    assert await buffer.due_chat_ids() == ("orphan",)
    assert await buffer.flush("orphan") is None

    assert await redis_client.zscore("buffer:deadlines", "orphan") is None


async def test_service_buffers_only_new_update(
    redis_client, database, clock
):
    service = MessageService(
        MessageRepository(database),
        MessageBuffer(redis_client, database, clock=clock),
        database,
    )
    message = incoming("5")

    assert await service.accept(message) is True
    assert await service.accept(message) is False

    assert await redis_client.llen("buffer:42") == 1
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM message_inbox"
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM task_outbox"
        ) == 0


async def test_service_falls_back_to_single_durable_task_when_redis_is_down(
    database, clock
):
    unavailable = redis.from_url(
        "redis://127.0.0.1:1/0",
        decode_responses=True,
        socket_connect_timeout=0.1,
    )
    service = MessageService(
        MessageRepository(database),
        MessageBuffer(unavailable, database, clock=clock),
        database,
    )
    try:
        assert await service.accept(incoming("6", "Не потерять")) is True
    finally:
        await unavailable.aclose()

    async with database.acquire() as connection:
        inbox_count = await connection.fetchval(
            "SELECT count(*) FROM message_inbox"
        )
        task = await connection.fetchrow(
            "SELECT kind, payload, idempotency_key FROM task_outbox"
        )
    assert inbox_count == 1
    assert task["kind"] == "process_message"
    assert json.loads(task["payload"]) == {
        "chat_id": "42",
        "update_ids": ["6"],
    }
    assert task["idempotency_key"] == "process_message:6"


async def test_service_falls_back_when_buffer_lock_is_busy(
    redis_client, database, clock
):
    held_lock = redis_client.lock("lock:buffer:42", timeout=10)
    assert await held_lock.acquire()
    service = MessageService(
        MessageRepository(database),
        MessageBuffer(redis_client, database, clock=clock),
        database,
    )
    try:
        assert await service.accept(incoming("7", "Не пропустить")) is True
    finally:
        await held_lock.release()

    assert await redis_client.llen("buffer:42") == 0
    async with database.acquire() as connection:
        task = await connection.fetchrow(
            "SELECT kind, idempotency_key FROM task_outbox"
        )
    assert tuple(task.values()) == ("process_message", "process_message:7")
