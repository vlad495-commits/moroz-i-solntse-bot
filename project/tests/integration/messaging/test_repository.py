import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.messaging.models import IncomingMessage
from moroz.messaging.repository import MessageRepository


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def message_repo(database):
    return MessageRepository(database)


@pytest.fixture
def incoming_message():
    return IncomingMessage(
        message_id="100",
        channel="telegram",
        chat_id="42",
        user_id="7",
        text="Привет",
        received_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        correlation_id=uuid4(),
    )


async def test_accept_same_message_once(message_repo, incoming_message):
    assert await message_repo.accept(incoming_message) is True
    assert await message_repo.accept(incoming_message) is False


async def test_enqueue_outbound_creates_one_message_and_separate_task(
    database, message_repo
):
    outbound_id = await message_repo.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Ответ",
        idempotency_key="reply:inbox-100",
    )
    repeated_id = await message_repo.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Ответ",
        idempotency_key="reply:inbox-100",
    )

    async with database.acquire() as connection:
        outbound = await connection.fetchrow(
            "SELECT id, status FROM outbound_messages WHERE id = $1",
            outbound_id,
        )
        task = await connection.fetchrow(
            "SELECT kind, payload, status FROM task_outbox"
        )
        counts = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM outbound_messages) AS outbound_count,
                (SELECT count(*) FROM task_outbox) AS task_count
            """
        )

    assert repeated_id == outbound_id
    assert tuple(outbound.values()) == (outbound_id, "pending")
    assert task["kind"] == "send_outbound"
    assert json.loads(task["payload"]) == {"outbound_id": str(outbound_id)}
    assert task["status"] == "pending"
    assert tuple(counts.values()) == (1, 1)
