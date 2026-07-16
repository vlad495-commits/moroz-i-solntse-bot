import asyncio

import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.messaging.outbox import OutboxRelay, enqueue_process_message


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=5)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


class RecordingQueue:
    def __init__(self, error=None):
        self.tasks = []
        self.error = error

    async def publish(self, task):
        self.tasks.append(task)
        if self.error:
            raise self.error


class BlockingQueue(RecordingQueue):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def publish(self, task):
        self.tasks.append(task)
        self.started.set()
        await self.release.wait()


async def test_relay_publishes_pending_task_then_marks_it_published(database):
    await enqueue_process_message(
        database,
        chat_id="42",
        update_ids=("10", "11"),
        text="Два сообщения",
    )
    queue = RecordingQueue()

    assert await OutboxRelay(database, queue).publish_pending() == 1

    assert len(queue.tasks) == 1
    assert queue.tasks[0].kind == "process_message"
    assert queue.tasks[0].payload["update_ids"] == ["10", "11"]
    assert queue.tasks[0].idempotency_key == "process_message:10,11"
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT status, published_at FROM task_outbox"
        )
    assert row["status"] == "published"
    assert row["published_at"] is not None


async def test_relay_leaves_task_pending_when_publish_fails(database):
    await enqueue_process_message(
        database,
        chat_id="42",
        update_ids=("12",),
        text="Повторить",
    )
    relay = OutboxRelay(database, RecordingQueue(RuntimeError("broker down")))

    with pytest.raises(RuntimeError, match="broker down"):
        await relay.publish_pending()

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT status, published_at FROM task_outbox"
        )
    assert tuple(row.values()) == ("pending", None)


async def test_concurrent_relays_do_not_publish_same_pending_row(database):
    await enqueue_process_message(
        database,
        chat_id="42",
        update_ids=("13",),
        text="Один publish",
    )
    queue = BlockingQueue()
    first = asyncio.create_task(OutboxRelay(database, queue).publish_pending())
    await queue.started.wait()

    second_count = await OutboxRelay(database, queue).publish_pending()
    queue.release.set()

    assert second_count == 0
    assert await first == 1
    assert len(queue.tasks) == 1
