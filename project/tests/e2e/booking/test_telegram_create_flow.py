import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram.types import ReplyKeyboardMarkup

from moroz.booking.catalog import CatalogService, CatalogStaff
from moroz.booking.dispatcher import MessageDispatcher
from moroz.booking.interaction import IntentVerdict
from moroz.booking.mock_catalog import MockBookingCatalog
from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import Slot
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.booking.workflow import BookingWorkflow
from moroz.booking.workflow_repository import BookingWorkflowRepository
from moroz.common.db import Database
from moroz.common.queue import QueueTask
from moroz.messaging.models import IncomingMessage
from moroz.messaging.repository import MessageRepository
from moroz.messaging.telegram import DeliveryResult, TelegramSender
from moroz.security.consent import ConsentService, PROCESSING_CONSENT_VERSION
from worker.main import MessageTaskHandler


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
PHONE = "+79990000000"


class FakeTelegram:
    def __init__(self):
        self.sent_messages = []

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return SimpleNamespace(message_id=701)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    value = Database(migrated_database_url, min_size=1, max_size=10)
    await value.connect()
    try:
        yield value
    finally:
        await value.close()


def _consultant_result():
    return SimpleNamespace(
        text="Консультация",
        prompt_tokens=5,
        completion_tokens=3,
        cached_tokens=0,
        total_tokens=8,
        model="fake-consultant",
    )


def _runtime(database):
    services = (
        CatalogService("1", "Крио", 30),
        CatalogService("2", "Массаж", 30),
    )
    staff = (CatalogStaff("7", "Анна", ("1", "2")),)
    catalog = MockBookingCatalog(services, staff, ("1", "2"), ("7",))
    port = MockYclientsAdapter(
        [
            Slot(
                "slot-a",
                ("1", "2"),
                "7",
                datetime(2026, 8, 2, 7, 0, tzinfo=UTC),
                60,
            )
        ]
    )
    workflow_repository = BookingWorkflowRepository(database, now=lambda: NOW)
    workflow = BookingWorkflow(
        catalog,
        port,
        workflow_repository,
        BookingService(port, BookingRepository(database), now=lambda: NOW),
        now=lambda: NOW,
    )
    router = AsyncMock(return_value=IntentVerdict("unknown", 0.0))
    consultant = AsyncMock(return_value=_consultant_result())
    dispatcher = MessageDispatcher(
        workflow_repository,
        workflow,
        router=router,
        consultant=consultant,
    )
    repository = MessageRepository(database)
    handler = MessageTaskHandler(
        database,
        consultant,
        TelegramSender(FakeTelegram(), repository),
        dispatcher=dispatcher,
        booking_interactions_enabled=True,
    )
    return SimpleNamespace(
        repository=repository,
        workflow_repository=workflow_repository,
        workflow=workflow,
        port=port,
        router=router,
        consultant=consultant,
        dispatcher=dispatcher,
        handler=handler,
    )


async def _process(
    runtime,
    *,
    owner: str,
    update_id: str,
    kind: str = "text",
    text: str = "",
    data=None,
):
    safe_text = text
    if kind == "callback":
        safe_text = "[booking callback]"
    elif kind == "contact":
        safe_text = "[contact shared]"
    accepted = await runtime.repository.accept(
        IncomingMessage(
            update_id=update_id,
            message_id=f"m-{update_id}",
            channel="telegram",
            chat_id=owner,
            user_id=owner,
            text=safe_text,
            received_at=NOW,
            correlation_id=uuid4(),
            kind=kind,
            data={} if data is None else data,
        )
    )
    assert accepted is True
    task = QueueTask(
        "process_message",
        {"chat_id": owner, "update_ids": [update_id]},
        f"process_message:{update_id}",
    )
    await runtime.handler.handle(task)
    async with runtime.repository._database.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT id, text, delivery_options
            FROM outbound_messages
            WHERE idempotency_key = $1
            """,
            f"reply:process_message:{update_id}",
        )
    assert row is not None
    options = row["delivery_options"]
    if isinstance(options, str):
        options = json.loads(options)
    return SimpleNamespace(id=row["id"], text=row["text"], options=options)


def _callback(reply, label):
    rows = reply.options["reply_markup"]["inline_keyboard"]
    for row in rows:
        for button in row:
            if label in button["text"]:
                return button["callback_data"]
    raise AssertionError(f"button not found: {label}")


async def _click(runtime, owner, update_id, reply, label):
    return await _process(
        runtime,
        owner=owner,
        update_id=update_id,
        kind="callback",
        data={"callback_data": _callback(reply, label)},
    )


async def _ready_confirmation(runtime, database, owner, prefix):
    reply = await _process(
        runtime,
        owner=owner,
        update_id=f"{prefix}-start",
        text="/book",
    )
    reply = await _click(runtime, owner, f"{prefix}-s1", reply, "Крио")
    reply = await _click(runtime, owner, f"{prefix}-s2", reply, "Массаж")
    reply = await _click(runtime, owner, f"{prefix}-done", reply, "Готово")
    reply = await _click(runtime, owner, f"{prefix}-master", reply, "Любой")
    reply = await _click(runtime, owner, f"{prefix}-date", reply, "02.08")
    reply = await _click(runtime, owner, f"{prefix}-slot", reply, "10:00")
    reply = await _process(
        runtime,
        owner=owner,
        update_id=f"{prefix}-name",
        text="Иван",
    )
    assert reply.options["reply_markup"]["keyboard"][0][0][
        "request_contact"
    ] is True
    await ConsentService(database).grant_processing_consent(
        "telegram",
        owner,
        PROCESSING_CONSENT_VERSION,
    )
    reply = await _process(
        runtime,
        owner=owner,
        update_id=f"{prefix}-contact",
        kind="contact",
        data={"phone_number": PHONE},
    )
    assert PHONE not in reply.text
    assert "+7******0000" in reply.text
    return reply


async def test_complete_mock_create_flow_persists_safe_history_and_keyboard(
    database,
    caplog,
):
    runtime = _runtime(database)
    summary = await _ready_confirmation(runtime, database, "101", "u1")
    confirmed = await _click(
        runtime,
        "101",
        "u1-confirm",
        summary,
        "Подтвердить",
    )

    assert confirmed.text == "Запись подтверждена."
    assert runtime.router.await_count == 0
    assert runtime.consultant.await_count == 0
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM bookings") == 1
        assert await connection.fetchval("SELECT count(*) FROM token_usage") == 0
        messages = await connection.fetch(
            "SELECT content FROM messages ORDER BY id"
        )
        outbounds = await connection.fetch(
            "SELECT text, delivery_options::text FROM outbound_messages"
        )
        contact_payload = await connection.fetchval(
            """
            SELECT payload::text FROM message_inbox
            WHERE external_message_id = 'u1-contact'
            """
        )
        state = await connection.fetchval(
            "SELECT state::text FROM booking_scenarios ORDER BY created_at LIMIT 1"
        )
    safe_material = json.dumps(
        [tuple(row.values()) for row in (*messages, *outbounds)],
        ensure_ascii=False,
    )
    assert PHONE not in safe_material
    assert PHONE not in contact_payload
    assert PHONE in state
    assert PHONE not in caplog.text


async def test_delivery_options_survive_outbox_and_telegram_sender_shape(database):
    runtime = _runtime(database)
    reply = await _process(
        runtime,
        owner="102",
        update_id="delivery-start",
        text="Записаться",
    )
    telegram = FakeTelegram()

    result = await TelegramSender(telegram, runtime.repository).send(reply.id)

    assert result == DeliveryResult.SENT
    markup = telegram.sent_messages[0]["reply_markup"].model_dump()
    assert markup["inline_keyboard"][0][0]["callback_data"].startswith(
        "booking:"
    )


async def test_router_timeout_durably_unblocks_inbox_and_sends_reply_keyboard(
    database,
):
    runtime = _runtime(database)
    runtime.router.side_effect = TimeoutError
    reply = await _process(
        runtime,
        owner="104",
        update_id="router-timeout",
        text="Неясный вопрос",
    )
    telegram = FakeTelegram()

    result = await TelegramSender(telegram, runtime.repository).send(reply.id)

    assert result == DeliveryResult.SENT
    assert isinstance(
        telegram.sent_messages[0]["reply_markup"],
        ReplyKeyboardMarkup,
    )
    assert runtime.consultant.await_count == 0
    async with database.acquire() as connection:
        assert await connection.fetchval(
            """
            SELECT status FROM message_inbox
            WHERE external_message_id = 'router-timeout'
            """
        ) == "processed"
        assert await connection.fetchval("SELECT count(*) FROM token_usage") == 0


async def test_duplicate_task_and_callback_do_not_duplicate_outbox_or_booking(
    database,
):
    runtime = _runtime(database)
    summary = await _ready_confirmation(runtime, database, "103", "dup")
    callback_data = _callback(summary, "Подтвердить")
    await _process(
        runtime,
        owner="103",
        update_id="dup-confirm",
        kind="callback",
        data={"callback_data": callback_data},
    )
    task = QueueTask(
        "process_message",
        {"chat_id": "103", "update_ids": ["dup-confirm"]},
        "process_message:dup-confirm",
    )

    await runtime.handler.handle(task)
    replay = await _process(
        runtime,
        owner="103",
        update_id="dup-replay",
        kind="callback",
        data={"callback_data": callback_data},
    )

    assert replay.text == "Запись подтверждена."
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM bookings") == 1
        assert await connection.fetchval(
            """
            SELECT count(*) FROM outbound_messages
            WHERE idempotency_key = 'reply:process_message:dup-confirm'
            """
        ) == 1


async def test_two_users_racing_same_mock_slot_get_one_confirmed_booking(database):
    runtime = _runtime(database)
    first = await _ready_confirmation(runtime, database, "201", "race-a")
    second = await _ready_confirmation(runtime, database, "202", "race-b")

    replies = await asyncio.gather(
        _click(runtime, "201", "race-a-confirm", first, "Подтвердить"),
        _click(runtime, "202", "race-b-confirm", second, "Подтвердить"),
    )

    assert sum(reply.text == "Запись подтверждена." for reply in replies) == 1
    loser = next(reply for reply in replies if reply.text != "Запись подтверждена.")
    assert "недоступ" in loser.text.lower()
    assert "подтверждена" not in loser.text.lower()
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM bookings") == 1


async def test_crash_after_workflow_checkpoint_resumes_without_duplicate(database):
    runtime = _runtime(database)

    class CrashAfterCheckpoint:
        async def dispatch(self, interaction, context, recent_count):
            await runtime.workflow.start_create(
                interaction.owner,
                interaction.idempotency_key,
            )
            raise RuntimeError("synthetic crash after workflow checkpoint")

    crashing = MessageTaskHandler(
        database,
        runtime.consultant,
        TelegramSender(FakeTelegram(), runtime.repository),
        dispatcher=CrashAfterCheckpoint(),
        booking_interactions_enabled=True,
    )
    assert await runtime.repository.accept(
        IncomingMessage(
            update_id="crash-start",
            message_id="m-crash",
            channel="telegram",
            chat_id="301",
            user_id="301",
            text="/book",
            received_at=NOW,
            correlation_id=uuid4(),
        )
    )
    task = QueueTask(
        "process_message",
        {"chat_id": "301", "update_ids": ["crash-start"]},
        "process_message:crash-start",
    )
    with pytest.raises(RuntimeError, match="synthetic crash"):
        await crashing.handle(task)

    await runtime.handler.handle(task)

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM booking_scenarios WHERE customer_id = '301'"
        ) == 1
        assert await connection.fetchval(
            """
            SELECT count(*) FROM outbound_messages
            WHERE idempotency_key = 'reply:process_message:crash-start'
            """
        ) == 1
        assert await connection.fetchval(
            """
            SELECT status FROM message_inbox
            WHERE external_message_id = 'crash-start'
            """
        ) == "processed"


async def test_contact_without_current_consent_fails_before_history_or_workflow(
    database,
    caplog,
):
    runtime = _runtime(database)
    assert await runtime.repository.accept(
        IncomingMessage(
            update_id="no-consent",
            message_id="m-no-consent",
            channel="telegram",
            chat_id="401",
            user_id="401",
            text="[contact shared]",
            received_at=NOW,
            correlation_id=uuid4(),
            kind="contact",
            data={"phone_number": PHONE},
        )
    )
    task = QueueTask(
        "process_message",
        {"chat_id": "401", "update_ids": ["no-consent"]},
        "process_message:no-consent",
    )

    await runtime.handler.handle(task)

    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM messages") == 0
        assert await connection.fetchval("SELECT count(*) FROM token_usage") == 0
        outbound = await connection.fetchrow(
            "SELECT text, delivery_options::text FROM outbound_messages"
        )
        payload = await connection.fetchval(
            """
            SELECT payload::text FROM message_inbox
            WHERE external_message_id = 'no-consent'
            """
        )
        assert await connection.fetchval("SELECT count(*) FROM booking_scenarios") == 0
    assert outbound is not None
    assert "обработать" in outbound["text"].lower()
    assert PHONE not in json.dumps(tuple(outbound.values()), ensure_ascii=False)
    assert PHONE not in payload
    assert PHONE not in caplog.text
