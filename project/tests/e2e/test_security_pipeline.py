import os
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
import redis.asyncio as redis
from httpx import ASGITransport, AsyncClient

from moroz.common.db import Database
from moroz.common.queue import QueueTask
from moroz.messaging.buffer import MessageBuffer
from moroz.messaging.models import IncomingMessage
from moroz.messaging.outbox import OutboxRelay, process_message_key
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import TelegramSender
from moroz.security.pipeline import INPUT_BLOCK_REPLY, SecurityPipeline
from moroz.security.validator import extract_structured_facts
from webhook import create_app
from worker.main import MessageTaskHandler, PipelinePump


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
WEBHOOK_SECRET = "test-webhook-secret"


class FakeTelegram:
    def __init__(self):
        self.sent_messages = []
        self.session = SimpleNamespace(close=self._close)

    async def _close(self):
        return None

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent_messages))


class RecordingQueue:
    def __init__(self):
        self.tasks = []

    async def publish(self, task):
        self.tasks.append(task)


class ForbiddenGateway:
    def __init__(self):
        self.calls = 0

    async def complete(self, _request):
        self.calls += 1
        raise AssertionError("local security decision must not call a provider")


def _incoming(update_id: str) -> IncomingMessage:
    return IncomingMessage(
        update_id=update_id,
        message_id=update_id,
        channel="telegram",
        chat_id="42",
        user_id="7",
        text=f"Сообщение {update_id}",
        received_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        correlation_id=uuid4(),
    )


def _telegram_update() -> dict:
    return {
        "update_id": 1200,
        "message": {
            "message_id": 1200,
            "date": 1_768_478_400,
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 7, "is_bot": False, "first_name": "Тест"},
            "text": "Текст до согласия",
        },
    }


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


async def test_worker_passes_exact_recent_count_and_persists_local_reply(database):
    repository = MessageRepository(database)
    update_ids = [str(1300 + index) for index in range(11)]
    for update_id in update_ids:
        assert await repository.accept(_incoming(update_id))

    gateway = ForbiddenGateway()
    pipeline = SecurityPipeline(gateway, "", extract_structured_facts(""))
    recent_counts = []

    async def secured_llm(text, context, *, recent_message_count):
        recent_counts.append(recent_message_count)
        return await pipeline.respond(
            text,
            context,
            recent_message_count=recent_message_count,
        )

    handler = MessageTaskHandler(
        database,
        secured_llm,
        TelegramSender(FakeTelegram(), repository),
    )
    await handler.handle(
        QueueTask(
            kind="process_message",
            payload={"chat_id": "42", "update_ids": update_ids},
            idempotency_key=process_message_key(update_ids),
        )
    )

    async with database.acquire() as connection:
        processed = await connection.fetchval(
            "SELECT count(*) FROM message_inbox WHERE status = 'processed'"
        )
        messages = await connection.fetch(
            "SELECT role, content FROM messages ORDER BY id"
        )
        outbound = await connection.fetchrow(
            "SELECT text, status FROM outbound_messages"
        )
        send_tasks = await connection.fetchval(
            "SELECT count(*) FROM task_outbox WHERE kind = 'send_outbound'"
        )

    assert recent_counts == [11]
    assert gateway.calls == 0
    assert processed == 11
    assert [row["role"] for row in messages] == ["user", "assistant"]
    assert messages[-1]["content"] == INPUT_BLOCK_REPLY
    assert tuple(outbound.values()) == (INPUT_BLOCK_REPLY, "pending")
    assert send_tasks == 1


async def test_pre_consent_update_has_no_inbox_history_security_or_provider_call(
    database,
    redis_client,
    migrated_database_url,
):
    telegram = FakeTelegram()
    app = create_app(
        database_url=migrated_database_url,
        redis_url=os.environ["REDIS_URL"],
        bot=telegram,
        webhook_secret=WEBHOOK_SECRET,
    )
    security_calls = 0
    provider = ForbiddenGateway()

    async def security_boundary(_text, _context, *, recent_message_count):
        nonlocal security_calls
        security_calls += 1
        return await SecurityPipeline(
            provider,
            "",
            extract_structured_facts(""),
        ).respond(
            "safe",
            [],
            recent_message_count=recent_message_count,
        )

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
        ) as client:
            response = await client.post("/telegram/webhook", json=_telegram_update())

        queue = RecordingQueue()
        repository = MessageRepository(database)
        await PipelinePump(
            MessageBuffer(redis_client, database),
            OutboxRelay(database, queue),
            repository,
        ).run_once()
        handler = MessageTaskHandler(
            database,
            security_boundary,
            TelegramSender(telegram, repository),
        )
        for task in queue.tasks:
            await handler.handle(task)

    async with database.acquire() as connection:
        inbox = await connection.fetchval("SELECT count(*) FROM message_inbox")
        history = await connection.fetchval("SELECT count(*) FROM messages")
        process_tasks = await connection.fetchval(
            "SELECT count(*) FROM task_outbox WHERE kind = 'process_message'"
        )

    assert response.status_code == 200
    assert inbox == history == process_tasks == 0
    assert security_calls == provider.calls == 0
