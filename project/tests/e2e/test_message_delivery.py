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
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from customer_data_deletion import delete_customer_data
from moroz.common.db import Database
from moroz.common.queue import QueueTask
from moroz.escalation.service import admin_reply_key
from moroz.messaging.buffer import BUFFER_TTL_SECONDS, MessageBuffer
from moroz.messaging.models import IncomingMessage
from moroz.messaging.outbox import OutboxRelay, enqueue_process_message
from moroz.messaging.repository import (
    MessageRepository,
    OutboundDeliveryBlocked,
)
from moroz.messaging.telegram import (
    DeliveryResult,
    TelegramSender,
    deliver_claimed_outbound,
)
from moroz.security.consent import ConsentService, PROCESSING_CONSENT_VERSION
from webhook import create_app
from worker.main import (
    MessageTaskHandler,
    PipelinePump,
    _acquire_worker_lock,
    _release_worker_lock,
)


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
WEBHOOK_SECRET = "test-webhook-secret"


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
        self.recent_counts = []

    async def __call__(self, text, context, *, recent_message_count=1):
        self.calls.append((text, context))
        self.recent_counts.append(recent_message_count)
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


async def test_claimed_outbound_deleted_before_fence_is_not_sent(
    database,
    redis_client,
):
    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Удалённый ответ",
        idempotency_key="reply:deleted-before-fence",
    )
    claimed = await repository.claim_outbound_delivery(outbound_id)
    assert claimed is not None
    deletion = await delete_customer_data(
        pool=database,
        redis_client=redis_client,
        chat_id=42,
        actor_id=1,
        ip_address=None,
        user_agent=None,
    )
    telegram = FakeTelegram()

    assert deletion.status == "deleted"
    assert await deliver_claimed_outbound(
        telegram,
        repository,
        claimed,
    ) == DeliveryResult.SKIPPED
    assert telegram.sent_messages == []


async def test_send_holds_customer_lock_until_provider_call_finishes(
    database,
    redis_client,
):
    class BlockingTelegram(FakeTelegram):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def send_message(self, **kwargs):
            self.sent_messages.append(kwargs)
            self.entered.set()
            await self.release.wait()
            return SimpleNamespace(message_id=701)

    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Успевший ответ",
        idempotency_key="reply:send-before-deletion",
    )
    claimed = await repository.claim_outbound_delivery(outbound_id)
    assert claimed is not None
    telegram = BlockingTelegram()
    send_task = asyncio.create_task(
        deliver_claimed_outbound(telegram, repository, claimed)
    )
    await asyncio.wait_for(telegram.entered.wait(), timeout=3)
    deletion_task = asyncio.create_task(
        delete_customer_data(
            pool=database,
            redis_client=redis_client,
            chat_id=42,
            actor_id=1,
            ip_address=None,
            user_agent=None,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert deletion_task.done() is False
        telegram.release.set()
        assert await asyncio.wait_for(send_task, timeout=3) == DeliveryResult.SENT
        assert (await asyncio.wait_for(deletion_task, timeout=3)).status == "deleted"
    finally:
        telegram.release.set()
        for task in (send_task, deletion_task):
            if not task.done():
                task.cancel()

    assert telegram.sent_messages == [{"chat_id": 42, "text": "Успевший ответ"}]


async def test_network_send_result_is_terminal_and_safe(
    database, caplog
):
    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Секретный ответ",
        idempotency_key="reply:unknown",
    )
    telegram = FakeTelegram(
        TelegramNetworkError(SimpleNamespace(), "sensitive failure detail")
    )
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


@pytest.mark.parametrize(
    "error",
    [
        TelegramRetryAfter(SimpleNamespace(), "retry later", 5),
        TelegramBadRequest(SimpleNamespace(), "invalid request"),
        RuntimeError("failed before send"),
    ],
)
async def test_definite_send_failure_is_released_for_queue_retry(database, error):
    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Ответ",
        idempotency_key=f"reply:definite:{type(error).__name__}",
    )

    with pytest.raises(type(error)):
        await TelegramSender(FakeTelegram(error), repository).send(outbound_id)

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT status FROM outbound_messages WHERE id = $1", outbound_id
        ) == "pending"


async def test_pre_send_validation_failure_releases_delivery(database):
    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="not-an-integer",
        text="Ответ",
        idempotency_key="reply:invalid-chat-id",
    )

    with pytest.raises(ValueError):
        await TelegramSender(FakeTelegram(), repository).send(outbound_id)

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT status FROM outbound_messages WHERE id = $1", outbound_id
        ) == "pending"


async def test_second_worker_cannot_reconcile_while_first_is_active(
    database, migrated_database_url
):
    second_database = Database(migrated_database_url, min_size=1, max_size=1)
    await second_database.connect()
    first_lock = await _acquire_worker_lock(database)
    try:
        with pytest.raises(RuntimeError, match="worker is already active"):
            await _acquire_worker_lock(second_database)
    finally:
        await _release_worker_lock(first_lock)

    second_lock = await _acquire_worker_lock(second_database)
    await _release_worker_lock(second_lock)
    await second_database.close()


async def test_concurrent_outbound_delivery_preserves_order_per_chat(database):
    repository = MessageRepository(database)
    first_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Первый",
        idempotency_key="reply:ordered:first",
    )
    second_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Второй",
        idempotency_key="reply:ordered:second",
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class OrderedTelegram(FakeTelegram):
        async def send_message(self, **kwargs):
            self.sent_messages.append(kwargs)
            if kwargs["text"] == "Первый":
                first_started.set()
                await release_first.wait()
            return SimpleNamespace(message_id=700 + len(self.sent_messages))

    telegram = OrderedTelegram()
    sender = TelegramSender(telegram, repository)
    first_delivery = asyncio.create_task(sender.send(first_id))
    await first_started.wait()

    with pytest.raises(OutboundDeliveryBlocked):
        await sender.send(second_id)

    release_first.set()
    assert await first_delivery == DeliveryResult.SENT
    assert await sender.send(second_id) == DeliveryResult.SENT
    assert [message["text"] for message in telegram.sent_messages] == [
        "Первый",
        "Второй",
    ]


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


async def test_process_message_passes_last_40_and_never_persists_compact_summary(
    database,
):
    repository = MessageRepository(database)
    assert await repository.accept(incoming())
    async with database.acquire() as connection:
        await connection.executemany(
            "INSERT INTO messages (chat_id, user_id, role, content) "
            "VALUES (42, 7, $1, $2)",
            [
                (
                    "user" if index % 2 == 0 else "assistant",
                    f"История {index}",
                )
                for index in range(45)
            ],
        )
    llm = FakeLLM()
    handler = MessageTaskHandler(
        database,
        llm,
        TelegramSender(FakeTelegram(), repository),
    )

    await handler.handle(
        QueueTask(
            kind="process_message",
            payload={"chat_id": "42", "update_ids": ["100"]},
            idempotency_key="process_message:100",
        )
    )

    current, context = llm.calls[0]
    assert current == "Новый вопрос"
    assert [item["content"] for item in context] == [
        f"История {index}" for index in range(5, 45)
    ]
    async with database.acquire() as connection:
        stored = await connection.fetch(
            "SELECT content FROM messages WHERE chat_id = 42 ORDER BY id"
        )
    contents = [row["content"] for row in stored]
    assert contents[-2:] == ["Новый вопрос", "Готовый ответ"]
    assert all("Сводка предыдущего диалога" not in text for text in contents)


async def test_human_mode_materializes_user_history_without_llm_or_reply(database):
    repository = MessageRepository(database)
    assert await repository.accept(incoming())
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id, enabled_at)
            VALUES ('42', true, 'low_feedback_rating', $1, now())
            """,
            uuid4(),
        )
    llm = FakeLLM()
    handler = MessageTaskHandler(
        database,
        llm,
        TelegramSender(FakeTelegram(), repository),
    )
    task = QueueTask(
        kind="process_message",
        payload={"chat_id": "42", "update_ids": ["100"]},
        idempotency_key="process_message:100",
    )

    await handler.handle(task)
    await handler.handle(task)

    async with database.acquire() as connection:
        messages = await connection.fetch(
            "SELECT role, content FROM messages ORDER BY id"
        )
        inbox_status = await connection.fetchval(
            "SELECT status FROM message_inbox WHERE external_message_id='100'"
        )
        counts = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM token_usage) AS usage,
                (SELECT count(*) FROM outbound_messages) AS outbound,
                (SELECT count(*) FROM task_outbox) AS tasks
            """
        )

    assert [tuple(row.values()) for row in messages] == [("user", "Новый вопрос")]
    assert inbox_status == "processed"
    assert tuple(counts.values()) == (0, 0, 0)
    assert llm.calls == []


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


async def test_process_message_without_inbox_is_acked_after_privacy_delete(
    database,
):
    llm = FakeLLM()
    repository = MessageRepository(database)
    handler = MessageTaskHandler(
        database, llm, TelegramSender(FakeTelegram(), repository)
    )

    await handler.handle(
        QueueTask(
            "process_message",
            {"update_ids": ["deleted-update"]},
            "process_message:deleted-update",
        )
    )

    assert llm.calls == []


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

    async def blocking_llm(text, context, *, recent_message_count=1):
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
        "update_ids": ["601"],
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
        webhook_secret=WEBHOOK_SECRET,
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
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
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


async def _seed_admin_handoff_reply(database, *, second_open=False):
    repository = MessageRepository(database)
    escalation_id = uuid4()
    reply_token = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            "INSERT INTO messages (chat_id, user_id, username, role, content) "
            "VALUES (42, 7, 'client', 'user', 'Нужна помощь')"
        )
        await connection.execute(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload)
            VALUES ($1, 'feedback', '42', 'open', 'private', '{}')
            """,
            escalation_id,
        )
        if second_open:
            await connection.execute(
                """
                INSERT INTO escalations
                    (id, source, customer_id, status, reason_code, payload)
                VALUES ($1, 'feedback', '42', 'open', 'other', '{}')
                """,
                uuid4(),
            )
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id, enabled_at)
            VALUES ('42', true, 'private', $1, now())
            """,
            escalation_id,
        )
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Ответ администратора",
        idempotency_key=admin_reply_key(escalation_id, reply_token),
    )
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO admin_audit_events
                (actor_id, action, object_type, object_id, after,
                 ip_address, user_agent)
            VALUES (
                7, 'escalation.reply_queued', 'escalation', $1,
                jsonb_build_object('outbound_id', $2::text, 'status', 'queued'),
                '127.0.0.1', 'test-agent'
            )
            """,
            str(escalation_id),
            str(outbound_id),
        )
    return repository, escalation_id, outbound_id


async def test_confirmed_admin_reply_materializes_history_and_resumes_bot(database):
    repository, escalation_id, outbound_id = await _seed_admin_handoff_reply(database)

    class Cache:
        def __init__(self):
            self.deleted = []

        async def delete(self, key):
            self.deleted.append(key)

    cache = Cache()
    sender = TelegramSender(FakeTelegram(), repository, context_cache=cache)

    assert await sender.send(outbound_id) == DeliveryResult.SENT
    assert await sender.send(outbound_id) == DeliveryResult.SKIPPED

    async with database.acquire() as connection:
        outbound = await connection.fetchrow(
            "SELECT status, external_message_id FROM outbound_messages WHERE id=$1",
            outbound_id,
        )
        messages = await connection.fetch(
            "SELECT user_id, username, role, content FROM messages ORDER BY id"
        )
        escalation = await connection.fetchrow(
            "SELECT status, resolved_at FROM escalations WHERE id=$1",
            escalation_id,
        )
        mode = await connection.fetchrow(
            """
            SELECT enabled,
                   expires_at > now() + interval '4 minutes' AS cooldown_started,
                   expires_at < now() + interval '6 minutes' AS cooldown_bounded
            FROM human_mode WHERE customer_id='42'
            """
        )
        audits = await connection.fetch(
            "SELECT action, object_type, object_id, before, after "
            "FROM admin_audit_events ORDER BY id"
        )

    assert tuple(outbound.values()) == ("sent", "701")
    assert [tuple(row.values()) for row in messages] == [
        (7, "client", "user", "Нужна помощь"),
        (7, "client", "assistant", "Ответ администратора"),
    ]
    assert escalation["status"] == "resolved"
    assert escalation["resolved_at"] is not None
    assert tuple(mode.values()) == (False, True, True)
    assert [row["action"] for row in audits] == [
        "escalation.reply_queued",
        "escalation.reply_delivered",
    ]
    assert all(row["object_type"] == "escalation" for row in audits)
    assert all(row["object_id"] == str(escalation_id) for row in audits)
    assert json.loads(audits[1]["before"]) == {"status": "queued"}
    assert json.loads(audits[1]["after"]) == {"status": "delivered"}
    assert "Ответ администратора" not in repr(audits)
    assert "customer_id" not in repr(audits)
    assert cache.deleted == ["chat:42:messages"]


async def test_confirmed_admin_reply_keeps_human_mode_for_other_open_handoff(
    database,
):
    repository, escalation_id, outbound_id = await _seed_admin_handoff_reply(
        database,
        second_open=True,
    )

    assert await TelegramSender(FakeTelegram(), repository).send(
        outbound_id
    ) == DeliveryResult.SENT

    async with database.acquire() as connection:
        mode = await connection.fetchrow(
            "SELECT enabled, expires_at FROM human_mode WHERE customer_id='42'"
        )
        open_count = await connection.fetchval(
            "SELECT count(*) FROM escalations WHERE customer_id='42' AND status='open'"
        )
        status = await connection.fetchval(
            "SELECT status FROM escalations WHERE id=$1",
            escalation_id,
        )
    assert tuple(mode.values()) == (True, None)
    assert open_count == 1
    assert status == "resolved"


async def test_unknown_admin_reply_delivery_keeps_handoff_open(database):
    repository, escalation_id, outbound_id = await _seed_admin_handoff_reply(database)
    telegram = FakeTelegram(
        TelegramNetworkError(SimpleNamespace(), "private network detail")
    )

    assert await TelegramSender(telegram, repository).send(
        outbound_id
    ) == DeliveryResult.DELIVERY_UNKNOWN

    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT
                (SELECT status FROM outbound_messages WHERE id=$1) AS outbound,
                (SELECT status FROM escalations WHERE id=$2) AS escalation,
                (SELECT enabled FROM human_mode WHERE customer_id='42') AS human,
                (SELECT count(*) FROM messages WHERE role='assistant') AS replies,
                (SELECT count(*) FROM admin_audit_events
                 WHERE action='escalation.reply_delivered') AS delivered_audits
            """,
            outbound_id,
            escalation_id,
        )
    assert tuple(state.values()) == ("delivery_unknown", "open", True, 0, 0)


async def test_admin_reply_completion_failure_rolls_back_all_side_effects(database):
    repository, escalation_id, outbound_id = await _seed_admin_handoff_reply(database)
    claimed = await repository.claim_outbound_delivery(outbound_id)
    assert claimed is not None
    async with database.acquire() as connection:
        await connection.execute(
            """
            CREATE FUNCTION reject_reply_delivered_audit() RETURNS trigger AS $$
            BEGIN
                IF NEW.action = 'escalation.reply_delivered' THEN
                    RAISE EXCEPTION 'forced delivered audit failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER reject_reply_delivered_audit
            BEFORE INSERT ON admin_audit_events
            FOR EACH ROW EXECUTE FUNCTION reject_reply_delivered_audit();
            """
        )

    with pytest.raises(Exception, match="forced delivered audit failure"):
        await repository.mark_outbound_sent(outbound_id, "701")

    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT
                (SELECT status FROM outbound_messages WHERE id=$1) AS outbound,
                (SELECT status FROM escalations WHERE id=$2) AS escalation,
                (SELECT enabled FROM human_mode WHERE customer_id='42') AS human,
                (SELECT count(*) FROM messages WHERE role='assistant') AS replies,
                (SELECT count(*) FROM admin_audit_events
                 WHERE action='escalation.reply_delivered') AS delivered_audits
            """,
            outbound_id,
            escalation_id,
        )
    assert tuple(state.values()) == ("sending", "open", True, 0, 0)


@pytest.mark.parametrize("broken_contract", ["chat_id", "queued_audit"])
async def test_admin_reply_completion_rejects_mismatched_contract(
    database,
    broken_contract,
):
    repository, escalation_id, outbound_id = await _seed_admin_handoff_reply(database)
    async with database.acquire() as connection:
        if broken_contract == "chat_id":
            await connection.execute(
                "UPDATE outbound_messages SET chat_id='43' WHERE id=$1",
                outbound_id,
            )
        else:
            await connection.execute(
                "DELETE FROM admin_audit_events WHERE object_id=$1",
                str(escalation_id),
            )
    assert await repository.claim_outbound_delivery(outbound_id) is not None

    with pytest.raises(ValueError, match="admin reply"):
        await repository.mark_outbound_sent(outbound_id, "701")

    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT
                (SELECT status FROM outbound_messages WHERE id=$1) AS outbound,
                (SELECT status FROM escalations WHERE id=$2) AS escalation,
                (SELECT enabled FROM human_mode WHERE customer_id='42') AS human,
                (SELECT count(*) FROM messages WHERE role='assistant') AS replies
            """,
            outbound_id,
            escalation_id,
        )
    assert tuple(state.values()) == ("sending", "open", True, 0)


async def test_malformed_admin_reply_key_fails_closed(database):
    repository = MessageRepository(database)
    outbound_id = await repository.enqueue_outbound(
        channel="telegram",
        chat_id="42",
        text="Не отправлять как обычное сообщение",
        idempotency_key="admin_handoff_reply:not-a-uuid:also-broken",
    )
    assert await repository.claim_outbound_delivery(outbound_id) is not None

    with pytest.raises(ValueError, match="admin reply key"):
        await repository.mark_outbound_sent(outbound_id, "701")

    async with database.acquire() as connection:
        status = await connection.fetchval(
            "SELECT status FROM outbound_messages WHERE id=$1",
            outbound_id,
        )
    assert status == "sending"


async def test_post_send_completion_failure_becomes_delivery_unknown(
    database, caplog
):
    repository, escalation_id, outbound_id = await _seed_admin_handoff_reply(database)
    async with database.acquire() as connection:
        await connection.execute(
            """
            CREATE FUNCTION reject_reply_delivered_audit() RETURNS trigger AS $$
            BEGIN
                IF NEW.action = 'escalation.reply_delivered' THEN
                    RAISE EXCEPTION 'forced delivered audit failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER reject_reply_delivered_audit
            BEFORE INSERT ON admin_audit_events
            FOR EACH ROW EXECUTE FUNCTION reject_reply_delivered_audit();
            """
        )

    result = await TelegramSender(FakeTelegram(), repository).send(outbound_id)

    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT
                (SELECT status FROM outbound_messages WHERE id=$1) AS outbound,
                (SELECT status FROM escalations WHERE id=$2) AS escalation,
                (SELECT enabled FROM human_mode WHERE customer_id='42') AS human,
                (SELECT count(*) FROM messages WHERE role='assistant') AS replies,
                (SELECT count(*) FROM admin_audit_events
                 WHERE action='escalation.reply_delivered') AS delivered_audits
            """,
            outbound_id,
            escalation_id,
        )
    assert result == DeliveryResult.DELIVERY_UNKNOWN
    assert tuple(state.values()) == ("delivery_unknown", "open", True, 0, 0)
    for private in (str(outbound_id), "42", "forced delivered audit failure"):
        assert private not in caplog.text


async def test_post_send_cancellation_becomes_delivery_unknown(
    database,
    monkeypatch,
):
    repository, escalation_id, outbound_id = await _seed_admin_handoff_reply(database)

    async def cancel_completion(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(repository, "mark_outbound_sent", cancel_completion)

    with pytest.raises(asyncio.CancelledError):
        await TelegramSender(FakeTelegram(), repository).send(outbound_id)

    async with database.acquire() as connection:
        state = await connection.fetchrow(
            """
            SELECT
                (SELECT status FROM outbound_messages WHERE id=$1) AS outbound,
                (SELECT status FROM escalations WHERE id=$2) AS escalation,
                (SELECT enabled FROM human_mode WHERE customer_id='42') AS human,
                (SELECT count(*) FROM messages WHERE role='assistant') AS replies
            """,
            outbound_id,
            escalation_id,
        )
    assert tuple(state.values()) == ("delivery_unknown", "open", True, 0)
