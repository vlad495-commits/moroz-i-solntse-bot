from datetime import UTC, datetime
from uuid import uuid4

import pytest

from config import BOT_PAUSE_KEY, MARKETING_DISABLED_REPLY
from tests.e2e import test_privacy_gate as privacy_fixtures
from tests.e2e.test_privacy_gate import (
    telegram_text_update, telegram_consent_callback, telegram_contact_update,
    grant_policy_consent,
)

client = privacy_fixtures.client
db = privacy_fixtures.db
fake_telegram = privacy_fixtures.fake_telegram
redis_client = privacy_fixtures.redis_client

pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


async def draft(db, phase="collecting", *, customer_id="42", step="service"):
    scenario_id = uuid4()
    await db.execute(
        "INSERT INTO booking_scenarios "
        "(id, kind, phase, idempotency_key, customer_id, state) "
        "VALUES ($1, 'create', $2, $3, $4, jsonb_build_object('step', $5::text))",
        scenario_id, phase, str(scenario_id), customer_id, step,
    )
    return scenario_id


@pytest.mark.parametrize("phase,step", [
    ("collecting", "service"), ("collecting", "catalog_category"),
    ("awaiting_confirmation", "confirm"),
])
async def test_exact_stop_closes_draft_without_processing_consent_even_paused(
    client, db, redis_client, fake_telegram, phase, step,
):
    scenario_id = await draft(db, phase, step=step)
    other = await draft(db, customer_id="99")
    await redis_client.set(BOT_PAUSE_KEY, "1")
    response = await client.post("/telegram/webhook", json=telegram_text_update(" СтОп! "))
    assert response.status_code == 200
    row = await db.fetchrow("SELECT phase, error_code FROM booking_scenarios WHERE id=$1", scenario_id)
    assert tuple(row.values()) == ("failed", "user_stop")
    assert await db.fetchval("SELECT phase FROM booking_scenarios WHERE id=$1", other) == "collecting"
    assert MARKETING_DISABLED_REPLY in fake_telegram.last_text
    assert "черновик" in fake_telegram.last_text.lower()
    assert await db.fetchval("SELECT active FROM marketing_consents") is False
    assert await db.fetchval("SELECT count(*) FROM booking_events WHERE scenario_id=$1 AND event_type='booking_flow_stopped'", scenario_id) == 1


@pytest.mark.parametrize("command", ["отписаться", "не писать", "не присылайте", "button"])
async def test_marketing_only_commands_keep_draft(client, db, fake_telegram, command):
    scenario_id = await draft(db)
    update = (telegram_consent_callback(data="marketing:disable") if command == "button"
              else telegram_text_update(command))
    assert (await client.post("/telegram/webhook", json=update)).status_code == 200
    assert await db.fetchval("SELECT phase FROM booking_scenarios WHERE id=$1", scenario_id) == "collecting"
    assert fake_telegram.last_text == MARKETING_DISABLED_REPLY


async def test_stop_retry_does_not_cancel_new_draft(client, db, fake_telegram):
    original = await draft(db)
    stop = telegram_text_update("stop")
    for _ in range(2):
        assert (await client.post("/telegram/webhook", json=stop)).status_code == 200
    new = await draft(db)
    assert (await client.post("/telegram/webhook", json=stop)).status_code == 200
    assert await db.fetchval("SELECT phase FROM booking_scenarios WHERE id=$1", original) == "failed"
    assert await db.fetchval("SELECT phase FROM booking_scenarios WHERE id=$1", new) == "collecting"
    assert len(fake_telegram.sent_messages) == 1


@pytest.mark.parametrize("phase", [None, "executing", "confirmed"])
async def test_stop_preserves_executing_and_real_booking(client, db, fake_telegram, phase):
    scenario_id = await draft(db, phase) if phase else None
    if phase == "confirmed":
        await db.execute(
            "INSERT INTO bookings (id,last_scenario_id,external_id,customer_id,booking_key,slot_id,starts_at,status,snapshot) "
            "VALUES ($1,$2,'real','42',$3,'slot',now(),'confirmed','{}')",
            uuid4(), scenario_id, uuid4(),
        )
    assert (await client.post("/telegram/webhook", json=telegram_text_update("стоп"))).status_code == 200
    if phase:
        assert await db.fetchval("SELECT phase FROM booking_scenarios WHERE id=$1", scenario_id) == phase
    if phase == "executing":
        assert "обрабатывается" in fake_telegram.last_text
    else:
        assert "нет незавершённого" in fake_telegram.last_text
    if phase == "confirmed":
        assert await db.fetchval("SELECT status FROM bookings WHERE external_id='real'") == "confirmed"


async def test_deletion_fence_blocks_stop_storage(client, db, redis_client):
    scenario_id = await draft(db)
    await redis_client.set("privacy:deleting:telegram:42", "1")
    assert (await client.post("/telegram/webhook", json=telegram_text_update("stop"))).status_code == 200
    assert await db.fetchval("SELECT phase FROM booking_scenarios WHERE id=$1", scenario_id) == "collecting"
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await db.fetchval("SELECT count(*) FROM marketing_consent_events") == 0


async def test_stop_blocks_old_booking_but_keeps_faq_and_fresh_booking(
    client, db, migrated_database_url, monkeypatch,
):
    from tests.e2e.booking.test_telegram_booking import _coordinator
    from tests.e2e.test_message_delivery import FakeLLM, FakeTelegram
    from moroz.messaging.models import IncomingMessage
    from moroz.messaging.repository import MessageRepository
    from moroz.messaging.router import RouteDecision
    from moroz.messaging.outbox import process_message_key
    from moroz.messaging.telegram import TelegramSender
    from moroz.common.queue import QueueTask
    from worker.main import MessageTaskHandler

    database, booking_repository, adapter, coordinator = await _coordinator(migrated_database_url)
    repository = MessageRepository(database)
    class Router(FakeLLM):
        async def __call__(self, text, context, **options):
            result = await super().__call__(text, context, recent_message_count=options.get("recent_message_count", 1))
            if text == "Хочу записаться":
                result.text = await options["dispatch"](RouteDecision("booking", 1, "create"))
            return result
    llm = Router()
    sender = TelegramSender(FakeTelegram(), repository)
    handler = MessageTaskHandler(database, llm, sender, booking_coordinator=coordinator)
    async def accept(update_id, text, *, kind="text", data=None, timestamp=1_768_478_400):
        await repository.accept_if_consented(IncomingMessage(
            str(update_id), str(update_id), "telegram", "42", "7", text,
            datetime.fromtimestamp(timestamp, UTC), uuid4(), kind, data or {},
        ))
    async def process(*ids):
        ids = list(map(str, ids))
        await handler.handle(QueueTask("process_message", {"chat_id": "42", "update_ids": ids}, process_message_key(ids)))
        for row in await db.fetch("SELECT id FROM outbound_messages WHERE status='pending' ORDER BY created_at,id"):
            await sender.send(row["id"])
    try:
        scenario_id = await draft(db)
        await accept(890, "Хочу записаться")
        await accept(891, "Как подготовиться?")
        await accept(892, "", kind="callback", data={"callback_data": f"booking:v1:{scenario_id.hex}:service:0"})
        await client.post("/telegram/webhook", json=telegram_text_update("stop", update_id=900))
        await process(890)
        await process(891)
        await process(892)
        assert await booking_repository.get_active_for_customer("42") is None
        assert any(call[0] == "Как подготовиться?" for call in llm.calls)
        await accept(901, "Хочу записаться")
        await process(901)
        new = await booking_repository.get_active_for_customer("42")
        assert new is not None and new.id != scenario_id
        # A first-seen older STOP must not close a draft originating in a newer event.
        await client.post("/telegram/webhook", json=telegram_text_update("stop", update_id=899))
        assert (await booking_repository.get_active_for_customer("42")).id == new.id
        # Late delivery after the new draft must still respect Telegram event order.
        await accept(889, "📅 Записаться")
        await accept(888, "", kind="callback", data={"callback_data": f"booking:v1:{scenario_id.hex}:service:0"})
        await accept(887, "", kind="contact", data={"contact_user_id": "7", "phone_number": "+79991234567"})
        await process(889)
        await process(888)
        await process(887)
        assert (await booking_repository.get_active_for_customer("42")).id == new.id
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
        # Redis can still contain text spanning the STOP boundary.
        await accept(886, "Как подготовиться?")
        await accept(904, "📅 Записаться")
        from uuid import UUID
        import moroz.messaging.outbox as outbox_module
        with monkeypatch.context() as patch:
            ids = iter([UUID(int=2), UUID(int=1), UUID(int=4), UUID(int=3)])
            patch.setattr(outbox_module, "uuid4", lambda: next(ids))
            await process(886, 904)
            await process(886, 904)
        tasks = await db.fetch("SELECT idempotency_key FROM task_outbox WHERE kind='process_message' ORDER BY created_at,id")
        assert [row["idempotency_key"] for row in tasks] == ["process_message:904", "process_message:886"]
        assert len(tasks) == 2
        # Deliberately deliver the newer subgroup first through the real retry envelope.
        from moroz.common.queue import RabbitQueue
        from tests.unit.common.test_queue_supervision import FakeMessage, FakeExchange
        queue = RabbitQueue("amqp://unused", retry_delays=(0, 0, 0))
        queue._exchange = FakeExchange()
        queue._dead_letter_exchange = FakeExchange()
        tail = FakeMessage()
        tail.body = QueueTask("process_message", {"update_ids": ["904"]}, "process_message:904").to_json().encode()
        await queue._handle(tail, handler.handle)
        assert tail.acked
        assert len(queue._exchange.messages) == 1
        assert queue._dead_letter_exchange.messages == []
        retry = queue._exchange.messages[0][0]
        assert retry.headers["x-retry-count"] == 1
        assert (await booking_repository.get_active_for_customer("42")).id == new.id
        assert await db.fetchval("SELECT status FROM message_inbox WHERE external_message_id='904'") == "accepted"
        await process(886)
        retry_delivery = FakeMessage(retry_count=1)
        retry_delivery.body = retry.body
        await queue._handle(retry_delivery, handler.handle)
        assert retry_delivery.acked
        assert len(queue._exchange.messages) == 1
        await process(904)
        assert (await booking_repository.get_active_for_customer("42")).id != new.id
        assert await db.fetchval("SELECT count(*) FROM message_inbox WHERE status='accepted'") == 0
        await client.post("/telegram/webhook", json=telegram_text_update("stop", update_id=950))
        reset_timestamp = 1_768_478_400 + 8 * 86400
        await accept(1, "📅 Записаться", timestamp=reset_timestamp)
        await process(1)
        reset_draft = await booking_repository.get_active_for_customer("42")
        assert reset_draft is not None
        # A current button from the fresh draft proves post-STOP origin despite lower IDs.
        await accept(2, "", kind="callback", timestamp=reset_timestamp, data={
            "callback_data": coordinator._callback(reset_draft, "service", 0),
        })
        await process(2)
        assert (await booking_repository.get_active_for_customer("42")).state["step"] == "staff"
        await accept(885, "", kind="callback", timestamp=reset_timestamp, data={
            "callback_data": f"booking:v1:{scenario_id.hex}:service:0",
        })
        await process(885)
        assert (await booking_repository.get_active_for_customer("42")).id == reset_draft.id
        assert (await booking_repository.get_active_for_customer("42")).state["step"] == "staff"
    finally:
        await database.close()


async def test_stop_order_allows_possible_week_reset_only_with_event_time():
    from moroz.messaging.booking_stop import before_stop
    marker = {"external_message_id": "900", "payload": {"received_at": "2026-09-01T10:00:00+00:00"}}
    assert before_stop("899", {"kind": "text", "received_at": "2026-09-01T10:00:01+00:00"}, marker)
    assert not before_stop("901", {"kind": "text", "received_at": "2026-09-01T10:00:00+00:00"}, marker)
    assert not before_stop("1", {"kind": "text", "received_at": "2026-09-09T10:00:00+00:00"}, marker)
    assert before_stop("1", {"kind": "callback", "received_at": "2026-09-09T10:00:00+00:00"}, marker)


async def test_stop_waits_for_scenario_execution_lock_then_rechecks_phase(client, db, fake_telegram):
    import asyncio
    scenario_id = await draft(db, "executing")
    lock_key = f"booking:scenario:{scenario_id}"
    await db.execute("SELECT pg_advisory_lock(hashtextextended($1,0))", lock_key)
    request = asyncio.create_task(client.post("/telegram/webhook", json=telegram_text_update("stop")))
    try:
        async with asyncio.timeout(5):
            while not await db.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_locks held JOIN pg_locks waiting "
                "ON held.locktype=waiting.locktype AND held.classid=waiting.classid AND held.objid=waiting.objid "
                "WHERE held.pid=pg_backend_pid() AND held.granted AND NOT waiting.granted)"
            ):
                await asyncio.sleep(0.01)
        await db.execute("UPDATE booking_scenarios SET phase='confirmed' WHERE id=$1", scenario_id)
    finally:
        await db.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", lock_key)
    assert (await asyncio.wait_for(request, 5)).status_code == 200
    assert await db.fetchval("SELECT phase FROM booking_scenarios WHERE id=$1", scenario_id) == "confirmed"
    assert "черновик закрыт" not in fake_telegram.last_text.lower()


async def test_stop_fails_closed_when_deletion_marker_cannot_be_read(client, db, monkeypatch):
    from redis.asyncio import Redis
    from redis.exceptions import RedisError
    scenario_id = await draft(db)
    original_get = Redis.get
    async def unavailable(self, key):
        if str(key).startswith("privacy:deleting:"):
            raise RedisError("fixture outage")
        return await original_get(self, key)
    monkeypatch.setattr(Redis, "get", unavailable)
    assert (await client.post("/telegram/webhook", json=telegram_text_update("stop"))).status_code == 200
    assert await db.fetchval("SELECT phase FROM booking_scenarios WHERE id=$1", scenario_id) == "collecting"
    assert await db.fetchval("SELECT count(*) FROM message_inbox") == 0
    assert await db.fetchval("SELECT count(*) FROM marketing_consent_events") == 0


async def test_contact_keeps_telegram_event_time_for_stop_order(client, db):
    import json
    await grant_policy_consent(client)
    update = telegram_contact_update(update_id=904)
    assert (await client.post("/telegram/webhook", json=update)).status_code == 200
    payload = json.loads(await db.fetchval("SELECT payload FROM message_inbox WHERE external_message_id='904'"))
    assert datetime.fromisoformat(payload["received_at"]).timestamp() == update["message"]["date"]


@pytest.mark.parametrize("booking_text", ["📅 Записаться", "Хочу записаться"])
async def test_delayed_stop_preserves_booking_started_by_last_message_in_batch(
    client, db, migrated_database_url, booking_text,
):
    from tests.e2e.booking.test_telegram_booking import _coordinator
    from tests.e2e.test_message_delivery import FakeLLM, FakeTelegram
    from moroz.messaging.models import IncomingMessage
    from moroz.messaging.repository import MessageRepository
    from moroz.messaging.router import RouteDecision
    from moroz.messaging.telegram import TelegramSender
    from moroz.common.queue import QueueTask
    from worker.main import MessageTaskHandler

    database, repository, adapter, coordinator = await _coordinator(migrated_database_url)
    messages = MessageRepository(database)
    class Router(FakeLLM):
        async def __call__(self, text, context, **options):
            result = await super().__call__(text, context, recent_message_count=1)
            result.text = await options["dispatch"](RouteDecision("booking", 1, "create"))
            return result
    sender = TelegramSender(FakeTelegram(), messages)
    handler = MessageTaskHandler(database, Router(), sender, booking_coordinator=coordinator)
    try:
        for update_id, text in [(890, "Как подготовиться?"), (901, booking_text)]:
            await messages.accept_if_consented(IncomingMessage(
                str(update_id), str(update_id), "telegram", "42", "7", text,
                datetime.fromtimestamp(1_768_478_400, UTC), uuid4(),
            ))
        task = QueueTask("process_message", {"update_ids": ["890", "901"]}, "process_message:890,901")
        await handler.handle(task)
        scenario = await repository.get_active_for_customer("42")
        assert scenario is not None
        assert scenario.idempotency_key == "telegram:create:890"
        for row in await db.fetch("SELECT id FROM outbound_messages WHERE status='pending' ORDER BY created_at,id"):
            await sender.send(row["id"])
        await client.post("/telegram/webhook", json=telegram_text_update("stop", update_id=900))
        current = await repository.get_active_for_customer("42")
        assert current is not None and current.id == scenario.id
        assert current.state["origin_update_id"] == "901"
        await handler.handle(task)
        assert await db.fetchval("SELECT count(*) FROM booking_scenarios") == 1
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()
