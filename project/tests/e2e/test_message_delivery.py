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
from httpx import ASGITransport, AsyncClient

from moroz.common.db import Database
from moroz.common.queue import QueueTask
from moroz.messaging.buffer import BUFFER_TTL_SECONDS, MessageBuffer
from moroz.messaging.models import IncomingMessage
from moroz.messaging.outbox import OutboxRelay, enqueue_process_message
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import DeliveryResult, TelegramSender
from moroz.security.consent import ConsentService, PROCESSING_CONSENT_VERSION
from webhook import create_app
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


async def test_fresh_worker_reconciles_stale_sending_without_resend(database):
    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Не повторять вслепую",
        idempotency_key="reply:stale-sending",
    )
    assert await repository.claim_outbound_delivery(outbound_id) is not None
    telegram = FakeTelegram()

    assert await MessageRepository(
        database
    ).reconcile_stale_outbound_deliveries() == 1
    assert await TelegramSender(
        telegram, MessageRepository(database)
    ).send(outbound_id) == DeliveryResult.SKIPPED

    assert telegram.sent_messages == []
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT status, claimed_at FROM outbound_messages WHERE id = $1",
            outbound_id,
        )
        assert tuple(row.values())[0] == "delivery_unknown"
        assert row["claimed_at"] is not None
        assert await connection.fetchval(
            "SELECT count(*) FROM task_outbox WHERE kind = 'send_outbound'"
        ) == 1


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


async def test_later_task_retries_until_earlier_accepted_update_is_processed(
    database,
):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("201", "Первый"))
    assert await repository.accept(incoming("202", "Второй"))
    llm = FakeLLM()
    handler = MessageTaskHandler(
        database, llm, TelegramSender(FakeTelegram(), repository)
    )
    first = QueueTask(
        "process_message",
        {"chat_id": "42", "update_ids": ["201"], "text": "tampered"},
        "process_message:201",
    )
    second = QueueTask(
        "process_message",
        {"chat_id": "42", "update_ids": ["202"], "text": "tampered"},
        "process_message:202",
    )

    with pytest.raises(ValueError, match="earlier accepted"):
        await handler.handle(second)
    await handler.handle(first)
    await handler.handle(second)

    assert [call[0] for call in llm.calls] == ["Первый", "Второй"]


@pytest.mark.parametrize("overlap_first", [False, True])
async def test_overlapping_tasks_feed_each_inbox_row_to_llm_once(
    database, overlap_first
):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("203", "Один"))
    assert await repository.accept(incoming("204", "Два"))
    llm = FakeLLM()
    handler = MessageTaskHandler(
        database, llm, TelegramSender(FakeTelegram(), repository)
    )
    single = QueueTask(
        "process_message",
        {"chat_id": "42", "update_ids": ["203"], "text": "ignored"},
        "process_message:203",
    )
    overlap = QueueTask(
        "process_message",
        {"chat_id": "42", "update_ids": ["203", "204"], "text": "ignored"},
        "process_message:203,204",
    )

    for task in ((overlap, single) if overlap_first else (single, overlap)):
        await handler.handle(task)

    assert [call[0] for call in llm.calls] == (
        ["Один\nДва"] if overlap_first else ["Один", "Два"]
    )
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM message_inbox WHERE status = 'processed'"
        ) == 2


async def test_process_message_uses_persisted_text_and_rejects_tampered_identity(
    database,
):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("205", "Текст из inbox"))
    llm = FakeLLM()
    handler = MessageTaskHandler(
        database, llm, TelegramSender(FakeTelegram(), repository)
    )

    with pytest.raises(ValueError, match="idempotency key"):
        await handler.handle(
            QueueTask(
                "process_message",
                {"chat_id": "42", "update_ids": ["205"], "text": "Подмена"},
                "process_message:wrong",
            )
        )
    with pytest.raises(ValueError, match="inbox rows"):
        await handler.handle(
            QueueTask(
                "process_message",
                {"chat_id": "99", "update_ids": ["205"], "text": "Подмена"},
                "process_message:205",
            )
        )
    await handler.handle(
        QueueTask(
            "process_message",
            {"chat_id": "42", "update_ids": ["205"], "text": "Подмена"},
            "process_message:205",
        )
    )

    assert llm.calls[0][0] == "Текст из inbox"


async def test_process_message_rejects_update_ids_outside_ingress_order(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("207", "Раньше"))
    assert await repository.accept(incoming("208", "Позже"))
    handler = MessageTaskHandler(
        database,
        FakeLLM(),
        TelegramSender(FakeTelegram(), repository),
    )

    with pytest.raises(ValueError, match="ingress order"):
        await handler.handle(
            QueueTask(
                "process_message",
                {
                    "chat_id": "42",
                    "update_ids": ["208", "207"],
                    "text": "ignored",
                },
                "process_message:208,207",
            )
        )


async def test_fully_processed_group_is_success_without_llm_or_outbound(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("206", "Уже обработано"))
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE message_inbox SET status = 'processed' "
            "WHERE external_message_id = '206'"
        )
    llm = FakeLLM()
    handler = MessageTaskHandler(
        database, llm, TelegramSender(FakeTelegram(), repository)
    )

    await handler.handle(
        QueueTask(
            "process_message",
            {"chat_id": "42", "update_ids": ["206"], "text": "ignored"},
            "process_message:206",
        )
    )

    assert llm.calls == []
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM outbound_messages") == 0


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
        MessageRepository(database),
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

    await PipelinePump(
        BrokenBuffer(),
        OutboxRelay(database, queue),
        MessageRepository(database),
    ).run_once()

    assert [task.idempotency_key for task in queue.tasks] == [
        "process_message:durable"
    ]


async def age_accepted(database, update_id, *, seconds=BUFFER_TTL_SECONDS + 1):
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE message_inbox "
            "SET created_at = now() - ($2 * interval '1 second') "
            "WHERE external_message_id = $1",
            update_id,
            seconds,
        )


async def test_fresh_pump_recovers_expired_inbox_with_due_redis_orphan(
    database, redis_client
):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("601", "Из сохранённого inbox"))
    await age_accepted(database, "601")
    await redis_client.zadd("buffer:deadlines", {"42": 0})
    queue = RecordingQueue()
    pump = PipelinePump(
        MessageBuffer(redis_client, database),
        OutboxRelay(database, queue),
        repository,
    )

    await pump.run_once()

    assert [task.idempotency_key for task in queue.tasks] == [
        "process_message:601"
    ]
    assert queue.tasks[0].payload == {
        "chat_id": "42",
        "update_ids": ["601"],
        "text": "Из сохранённого inbox",
    }
    assert await redis_client.zscore("buffer:deadlines", "42") is None


async def test_fresh_pump_recovers_expired_inbox_after_full_redis_loss(
    database, redis_client
):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("602", "Redis всё потерял"))
    await age_accepted(database, "602")
    await redis_client.flushdb()
    queue = RecordingQueue()

    await PipelinePump(
        MessageBuffer(redis_client, database),
        OutboxRelay(database, queue),
        repository,
    ).run_once()

    assert [task.idempotency_key for task in queue.tasks] == [
        "process_message:602"
    ]


async def test_recovery_sweep_is_idempotent_and_bounded(
    database, redis_client
):
    repository = MessageRepository(database)
    for index in range(101):
        assert await repository.accept(
            incoming(str(700 + index), f"Сообщение {index}")
        )
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE message_inbox "
            "SET created_at = now() - interval '31 seconds'"
        )
    queue = RecordingQueue()
    pump = PipelinePump(
        MessageBuffer(redis_client, database),
        OutboxRelay(database, queue),
        repository,
    )

    await pump.run_once()
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM task_outbox "
            "WHERE kind = 'process_message'"
        ) == 100
    await pump.run_once()

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM task_outbox "
            "WHERE kind = 'process_message'"
        ) == 101
    assert len({task.idempotency_key for task in queue.tasks}) == 101


async def test_recovery_sweep_does_not_steal_active_buffer_row(
    database, redis_client
):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("901", "Ещё в активном буфере"))
    queue = RecordingQueue()

    await PipelinePump(
        MessageBuffer(redis_client, database),
        OutboxRelay(database, queue),
        repository,
    ).run_once()

    assert queue.tasks == []
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM task_outbox "
            "WHERE kind = 'process_message'"
        ) == 0


async def test_redis_loss_recovery_processes_persisted_message_once(
    database, redis_client
):
    repository = MessageRepository(database)
    assert await repository.accept(incoming("902", "Только один LLM вызов"))
    await age_accepted(database, "902")
    await redis_client.flushdb()
    queue = RecordingQueue()
    await PipelinePump(
        MessageBuffer(redis_client, database),
        OutboxRelay(database, queue),
        repository,
    ).run_once()
    llm = FakeLLM()
    handler = MessageTaskHandler(
        database,
        llm,
        TelegramSender(FakeTelegram(), repository),
    )

    await handler.handle(queue.tasks[0])
    await handler.handle(queue.tasks[0])

    assert [call[0] for call in llm.calls] == ["Только один LLM вызов"]
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM outbound_messages"
        ) == 1
        assert await connection.fetchval(
            "SELECT status FROM message_inbox "
            "WHERE external_message_id = '902'"
        ) == "processed"


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


async def test_duplicate_consented_webhook_update_crosses_pipeline_once(
    database, redis_client, migrated_database_url
):
    telegram = FakeTelegram()
    llm = FakeLLM()
    await ConsentService(database).grant_processing_consent(
        "telegram", "7", PROCESSING_CONSENT_VERSION
    )
    app = create_app(
        database_url=migrated_database_url,
        redis_url=os.environ["REDIS_URL"],
        bot=telegram,
    )
    update = {
        "update_id": 990,
        "message": {
            "message_id": 100,
            "date": 1_768_478_400,
            "chat": {"id": 42, "type": "private"},
            "from": {
                "id": 7,
                "is_bot": False,
                "first_name": "Тест",
            },
            "text": "Один вопрос",
        },
    }

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = await client.post("/telegram/webhook", json=update)
            duplicate = await client.post("/telegram/webhook", json=update)

        await redis_client.zadd("buffer:deadlines", {"42": 0})
        queue = RecordingQueue()
        repository = MessageRepository(database)
        pump = PipelinePump(
            MessageBuffer(redis_client, database),
            OutboxRelay(database, queue),
            repository,
        )
        handler = MessageTaskHandler(
            database, llm, TelegramSender(telegram, repository)
        )

        await pump.run_once()
        process_task = next(
            task for task in queue.tasks if task.kind == "process_message"
        )
        await handler.handle(process_task)
        await handler.handle(process_task)
        await pump.run_once()
        send_task = next(
            task for task in queue.tasks if task.kind == "send_outbound"
        )
        await handler.handle(send_task)
        await handler.handle(send_task)

    async with database.acquire() as connection:
        inbox_count = await connection.fetchval("SELECT count(*) FROM message_inbox")
        outbound_count = await connection.fetchval(
            "SELECT count(*) FROM outbound_messages"
        )

    assert first.status_code == duplicate.status_code == 200
    assert inbox_count == outbound_count == 1
    assert len(llm.calls) == 1
    assert len(telegram.sent_messages) == 1
