import json
import os
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio
import redis.asyncio as redis
from httpx import ASGITransport, AsyncClient

from moroz.common.db import Database
from moroz.common.queue import QueueTask
from worker.main import MessageTaskHandler
from webhook import create_app


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio

WEBHOOK_SECRET = "test-webhook-secret"
SECRET = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
RECEIVED_AT = datetime(2026, 8, 1, 12, 30, tzinfo=UTC)


class FrozenClock:
    def now(self):
        return RECEIVED_AT


class FakeSession:
    async def close(self):
        pass


class FakeTelegram:
    def __init__(self):
        self.session = FakeSession()
        self.sent_messages = []
        self.chat_actions = []
        self.answered_callback_queries = []
        self.answer_error = None

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=700 + len(self.sent_messages))

    async def send_chat_action(self, **kwargs):
        self.chat_actions.append(kwargs)
        return True

    async def answer_callback_query(self, callback_query_id):
        self.answered_callback_queries.append(callback_query_id)
        if self.answer_error:
            raise self.answer_error
        return True


def booking_callback(
    data="booking:opaque123",
    *,
    update_id=1001,
    chat_id=10,
    chat_type="private",
    user_id=10,
):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": "Test",
            },
            "chat_instance": "test-chat",
            "data": data,
            "message": {
                "message_id": 99,
                "date": 1_768_478_400,
                "chat": {"id": chat_id, "type": chat_type},
            },
        },
    }


def contact_update(
    *,
    sender_id=10,
    contact_user_id=10,
    update_id=1002,
    chat_id=10,
    chat_type="private",
    phone_number="+79990000000",
):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 100,
            "date": 1_768_478_400,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {
                "id": sender_id,
                "is_bot": False,
                "first_name": "Test",
            },
            "contact": {
                "phone_number": phone_number,
                "first_name": "Test",
                "user_id": contact_user_id,
            },
        },
    }


@pytest.fixture
def fake_telegram():
    return FakeTelegram()


@pytest_asyncio.fixture
async def database(migrated_database_url):
    connection = await asyncpg.connect(migrated_database_url)
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def worker_database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
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


@pytest_asyncio.fixture
async def client(migrated_database_url, fake_telegram, redis_client):
    app = create_app(
        database_url=migrated_database_url,
        redis_url=os.environ["REDIS_URL"],
        bot=fake_telegram,
        webhook_secret=WEBHOOK_SECRET,
        clock=FrozenClock(),
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=SECRET,
        ) as http_client:
            yield http_client


async def grant_processing_consent(database, user_id=10):
    await database.execute(
        """
        INSERT INTO processing_consents (channel, user_id, consent_version)
        VALUES ('telegram', $1, 'v1')
        """,
        str(user_id),
    )


async def fetch_inbox(database):
    row = await database.fetchrow(
        """
        SELECT external_message_id, payload
        FROM message_inbox
        ORDER BY ingress_sequence
        """
    )
    payload = row["payload"]
    return {
        "external_message_id": row["external_message_id"],
        "payload": json.loads(payload) if isinstance(payload, str) else payload,
    }


async def fetch_process_task(database):
    row = await database.fetchrow(
        """
        SELECT payload, idempotency_key
        FROM task_outbox
        WHERE kind = 'process_message'
        """
    )
    payload = row["payload"]
    return (
        json.loads(payload) if isinstance(payload, str) else payload,
        row["idempotency_key"],
    )


async def test_booking_callback_is_accepted_into_inbox_without_direct_execution(
    client, database, redis_client, fake_telegram
):
    await grant_processing_consent(database)

    response = await client.post(
        "/telegram/webhook",
        json=booking_callback(),
    )

    row = await fetch_inbox(database)
    assert response.status_code == 200
    assert row["external_message_id"] == "1001"
    assert row["payload"]["kind"] == "callback"
    assert row["payload"]["data"] == {
        "callback_data": "booking:opaque123"
    }
    assert row["payload"]["received_at"] == RECEIVED_AT.isoformat()
    assert "opaque123" not in row["payload"]["text"]
    assert await fetch_process_task(database) == (
        {"chat_id": "10", "update_ids": ["1001"]},
        "process_message:1001",
    )
    assert await redis_client.llen("buffer:10") == 0
    assert fake_telegram.sent_messages == []
    assert fake_telegram.chat_actions == []
    assert fake_telegram.answered_callback_queries == ["callback-1001"]


async def test_duplicate_booking_callback_keeps_single_inbox_row_and_task(
    client, database, fake_telegram
):
    await grant_processing_consent(database)
    update = booking_callback(update_id=1003)

    assert (await client.post("/telegram/webhook", json=update)).status_code == 200
    assert (await client.post("/telegram/webhook", json=update)).status_code == 200

    assert await database.fetchval("SELECT count(*) FROM message_inbox") == 1
    assert await database.fetchval(
        "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
    ) == 1
    assert fake_telegram.answered_callback_queries == [
        "callback-1003",
        "callback-1003",
    ]


async def test_callback_ack_failure_does_not_undo_durable_accept(
    client, database, fake_telegram, caplog
):
    await grant_processing_consent(database)
    fake_telegram.answer_error = TimeoutError("opaque123 private failure")

    response = await client.post(
        "/telegram/webhook",
        json=booking_callback(update_id=1006),
    )

    assert response.status_code == 200
    assert await database.fetchval("SELECT count(*) FROM message_inbox") == 1
    assert await database.fetchval(
        "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
    ) == 1
    assert fake_telegram.answered_callback_queries == ["callback-1006"]
    assert "telegram_callback_ack_failed error_type=TimeoutError" in caplog.text
    assert "opaque123" not in caplog.text
    assert "private failure" not in caplog.text


async def test_own_contact_is_accepted_without_placing_pii_in_display_text(
    client, database, redis_client
):
    await grant_processing_consent(database)

    response = await client.post(
        "/telegram/webhook",
        json=contact_update(),
    )

    row = await fetch_inbox(database)
    assert response.status_code == 200
    assert row["payload"]["kind"] == "contact"
    assert row["payload"]["data"] == {"phone_number": "+79990000000"}
    assert "+79990000000" not in row["payload"]["text"]
    assert await fetch_process_task(database) == (
        {"chat_id": "10", "update_ids": ["1002"]},
        "process_message:1002",
    )
    assert await redis_client.llen("buffer:10") == 0


async def test_contact_must_belong_to_sender(client, database):
    await grant_processing_consent(database)

    response = await client.post(
        "/telegram/webhook",
        json=contact_update(sender_id=10, contact_user_id=11),
    )

    assert response.status_code == 200
    assert await database.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await database.fetchval("SELECT count(*) FROM task_outbox") == 0


@pytest.mark.parametrize(
    "update",
    [booking_callback(update_id=1004), contact_update(update_id=1005)],
)
async def test_booking_interaction_requires_processing_consent(
    client, database, fake_telegram, update
):
    response = await client.post("/telegram/webhook", json=update)

    assert response.status_code == 200
    assert await database.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await database.fetchval(
        "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
    ) == 0
    assert len(fake_telegram.sent_messages) == 1
    expected_answers = (
        [update["callback_query"]["id"]]
        if "callback_query" in update
        else []
    )
    assert fake_telegram.answered_callback_queries == expected_answers


async def test_old_worker_fails_closed_for_contact_without_leaking_or_mutating(
    client,
    database,
    worker_database,
    fake_telegram,
    caplog,
):
    await grant_processing_consent(database)
    assert (
        await client.post(
            "/telegram/webhook",
            json=contact_update(update_id=1007),
        )
    ).status_code == 200
    task = await database.fetchrow(
        """
        SELECT payload, idempotency_key
        FROM task_outbox
        WHERE kind = 'process_message'
        """
    )
    task_payload = task["payload"]
    if isinstance(task_payload, str):
        task_payload = json.loads(task_payload)
    llm = AsyncMock()
    handler = MessageTaskHandler(worker_database, llm, object())

    with pytest.raises(
        RuntimeError,
        match="non-text interaction requires structured dispatcher",
    ):
        await handler.handle(
            QueueTask(
                kind="process_message",
                payload=task_payload,
                idempotency_key=task["idempotency_key"],
            )
        )

    llm.assert_not_awaited()
    assert await database.fetchval(
        "SELECT status FROM message_inbox WHERE external_message_id = '1007'"
    ) == "accepted"
    assert await database.fetchval("SELECT count(*) FROM messages") == 0
    assert await database.fetchval("SELECT count(*) FROM outbound_messages") == 0
    assert "+79990000000" not in caplog.text
    assert "[shared contact]" not in caplog.text
