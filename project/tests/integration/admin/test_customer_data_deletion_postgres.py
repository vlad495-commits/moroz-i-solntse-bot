import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
import redis.asyncio as redis

from customer_data_deletion import CustomerDataDeletionError, delete_customer_data
from moroz.booking.projection import PROJECTION_LOCK, ProjectionRepository
from moroz.booking.yclients_records import ProjectionRecord, ProjectionSnapshot
from moroz.common.db import Database
from moroz.messaging.models import IncomingMessage
from moroz.messaging.repository import MessageRepository
from moroz.common.queue import QueueTask
from moroz.notifications.repository import SchedulerJobRepository
from worker.main import MessageTaskHandler


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


async def test_deletes_target_postgres_and_redis_but_preserves_control(
    migrated_database_url,
):
    conn = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=2)
    await pool.connect()
    cache = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await cache.flushdb()
    scenario_id = uuid4()
    booking_id = uuid4()
    booking_key = uuid4()
    event_id = uuid4()
    outbound_id = uuid4()
    staff_outbound_id = uuid4()
    control_staff_outbound_id = uuid4()
    reactivation_campaign_id = uuid4()
    try:
        target_message_id = await conn.fetchval(
            "INSERT INTO messages "
            "(chat_id, user_id, username, role, content, llm_usage_tracked) "
            "VALUES (42, 7, 'target-user', 'user', 'secret', true) RETURNING id"
        )
        control_message_id = await conn.fetchval(
            "INSERT INTO messages "
            "(chat_id, user_id, username, role, content, llm_usage_tracked) "
            "VALUES (84, 8, 'control-user', 'user', 'keep', true) RETURNING id"
        )
        await conn.execute(
            "INSERT INTO token_usage (chat_id, user_id, source_message_id, model) "
            "VALUES (42, 7, $1, 'test'), (84, 8, $2, 'test')",
            target_message_id,
            control_message_id,
        )
        await conn.execute(
            "INSERT INTO processing_consents (channel, user_id, consent_version) "
            "VALUES ('telegram', '7', 'v1'), ('telegram', '8', 'v1')"
        )
        await conn.execute(
            """
            INSERT INTO marketing_consents
                (id, channel, user_id, consent_version, active, granted_at)
            VALUES ($1, 'telegram', '42', 'marketing-v1', true, now()),
                   ($2, 'telegram', '84', 'marketing-v1', true, now())
            """,
            uuid4(),
            uuid4(),
        )
        await conn.execute(
            """
            INSERT INTO reactivation_campaigns
                (id, segment, status, after_visit_days, sleeping_days,
                 discount_percent, base_offer, llm_instruction,
                 recipient_count, queued_at)
            VALUES ($1, 'sleeping', 'queued', 1, 90, 0, '', '', 2, now())
            """,
            reactivation_campaign_id,
        )
        await conn.execute(
            """
            INSERT INTO reactivation_deliveries
                (id, campaign_id, channel, user_id, status)
            VALUES ($1, $2, 'telegram', '42', 'queued'),
                   ($3, $2, 'telegram', '84', 'queued')
            """,
            uuid4(),
            reactivation_campaign_id,
            uuid4(),
        )
        await conn.execute(
            """
            INSERT INTO message_inbox
                (id, channel, external_message_id, chat_id, payload, correlation_id)
            VALUES ($1, 'telegram', 'target-update', '42',
                    '{"user_id":"7","text":"secret"}'::jsonb, $2),
                   ($3, 'telegram', 'control-update', '84',
                    '{"user_id":"8","text":"keep"}'::jsonb, $4)
            """,
            uuid4(), uuid4(), uuid4(), uuid4(),
        )
        await conn.execute(
            """
            INSERT INTO outbound_messages
                (id, channel, chat_id, text, idempotency_key)
            VALUES ($1, 'telegram', '42', 'secret reply', 'target-outbound'),
                   ($2, 'telegram', '84', 'keep reply', 'control-outbound'),
                   ($3, 'telegram', '999', 'external-secret', $4),
                   ($5, 'telegram', '999', 'keep staff', $6)
            """,
            outbound_id,
            uuid4(),
            staff_outbound_id,
            f"staff:{booking_key}:test",
            control_staff_outbound_id,
            f"staff:{uuid4()}:control",
        )
        await conn.execute(
            """
            INSERT INTO task_outbox (id, kind, payload, idempotency_key)
            VALUES ($1, 'process_message',
                    '{"update_ids":["target-update"]}'::jsonb, 'target-task'),
                   ($2, 'send_outbound', $3::jsonb, 'target-send'),
                   ($4, 'process_message', '{"chat_id":"84"}'::jsonb, 'control-task'),
                   ($5, 'send_outbound', $6::jsonb, 'staff-send')
            """,
            uuid4(),
            uuid4(),
            json.dumps({"outbound_id": str(outbound_id)}),
            uuid4(),
            uuid4(),
            json.dumps({"outbound_id": str(staff_outbound_id)}),
        )
        await conn.execute(
            """
            INSERT INTO booking_scenarios
                (id, kind, phase, idempotency_key, customer_id, state)
            VALUES ($1, 'create', 'confirmed', 'target-scenario', '42',
                    '{"phone":"secret"}'::jsonb)
            """,
            scenario_id,
        )
        await conn.execute(
            """
            INSERT INTO bookings
                (id, last_scenario_id, external_id, customer_id, slot_id,
                 starts_at, status, snapshot, booking_key)
            VALUES ($1, $2, '401', '42', 'slot', now(),
                    'confirmed', '{"phone":"secret"}'::jsonb, $3)
            """,
            booking_id, scenario_id, booking_key,
        )
        await conn.execute(
            """
            INSERT INTO yclients_booking_projection
                (external_id, booking_key, bot_marker_state, starts_at,
                 scheduled_end_at, status, deleted, client_name, staff_name,
                 service_names, synced_at)
            VALUES ('401', $1, 'valid', now(), NULL, 'confirmed', false,
                    'Клиент', 'Мастер', ARRAY['Услуга'], now())
            """,
            booking_key,
        )
        await conn.execute(
            """
            CREATE FUNCTION reject_deletion_provider_mutation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'provider mutation enqueued';
            END;
            $$;
            CREATE TRIGGER reject_deletion_provider_mutation_insert
            BEFORE INSERT ON task_outbox
            FOR EACH ROW EXECUTE FUNCTION reject_deletion_provider_mutation();
            """
        )
        await conn.execute(
            """
            INSERT INTO booking_events (id, scenario_id, event_type, payload)
            VALUES ($1, $2, 'confirmed', '{"secret":true}'::jsonb)
            """,
            event_id, scenario_id,
        )
        await conn.execute(
            """
            INSERT INTO scheduler_jobs
                (id, kind, run_at, payload, idempotency_key, status, booking_key)
            VALUES ($1, 'reminder', now(), '{"customer_id":"42"}'::jsonb,
                    'target-job', 'pending', $2)
            """,
            uuid4(), booking_key,
        )
        await conn.execute(
            "INSERT INTO notification_feedback_requests "
            "(id, customer_id, booking_key, requested_at) "
            "VALUES ($1, '42', $2, now())",
            uuid4(), booking_key,
        )
        escalation_id = uuid4()
        await conn.execute(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload)
            VALUES ($1, 'booking', '42', 'open', 'test', '{"secret":true}')
            """,
            escalation_id,
        )
        await conn.execute(
            "INSERT INTO human_mode "
            "(customer_id, enabled, reason_code, escalation_id, enabled_at) "
            "VALUES ('42', true, 'test', $1, now())",
            escalation_id,
        )

        await cache.rpush("chat:42:messages", "secret")
        await cache.rpush("buffer:42", "secret")
        await cache.zadd("buffer:deadlines", {"42": 1, "84": 1})
        await cache.set("consent:state:telegram:42:7", "pii")
        await cache.set("bot:paused", "1")

        result = await delete_customer_data(
            pool=pool,
            redis_client=cache,
            chat_id=42,
            actor_id=1,
            ip_address="127.0.0.1",
            user_agent="pytest",
        )

        assert result.status == "deleted"
        for table, predicate in {
            "messages": "chat_id = 42",
            "token_usage": "chat_id = 42",
            "message_inbox": "channel = 'telegram' AND chat_id = '42'",
            "outbound_messages": "channel = 'telegram' AND chat_id = '42'",
            "processing_consents": "channel = 'telegram' AND user_id = '7'",
            "marketing_consents": "channel = 'telegram' AND user_id = '42'",
            "reactivation_deliveries": "channel = 'telegram' AND user_id = '42'",
            "booking_scenarios": "customer_id = '42'",
            "bookings": "customer_id = '42'",
            "notification_feedback_requests": "customer_id = '42'",
            "escalations": "customer_id = '42'",
            "human_mode": "customer_id = '42'",
        }.items():
            assert await conn.fetchval(f"SELECT count(*) FROM {table} WHERE {predicate}") == 0
        assert await conn.fetchval("SELECT count(*) FROM booking_events WHERE id = $1", event_id) == 0
        assert await conn.fetchval("SELECT count(*) FROM scheduler_jobs WHERE booking_key = $1", booking_key) == 0
        assert await conn.fetchval("SELECT count(*) FROM task_outbox WHERE idempotency_key LIKE 'target-%'") == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM outbound_messages WHERE id = $1",
            staff_outbound_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM outbound_messages WHERE id = $1",
            control_staff_outbound_id,
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM task_outbox WHERE idempotency_key = 'staff-send'"
        ) == 0
        assert await conn.fetchval("SELECT count(*) FROM messages WHERE chat_id = 84") == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM messages WHERE id = $1", target_message_id
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM token_usage WHERE source_message_id = $1",
            target_message_id,
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM token_usage WHERE source_message_id = $1",
            control_message_id,
        ) == 1
        assert await conn.fetchval("SELECT count(*) FROM processing_consents WHERE user_id = '8'") == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM marketing_consents WHERE user_id = '84'"
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM reactivation_deliveries WHERE user_id = '84'"
        ) == 1

        assert await conn.fetchval(
            "SELECT count(*) FROM yclients_booking_projection "
            "WHERE external_id = '401'"
        ) == 0
        stored_suppression = json.loads(
            await conn.fetchval(
                "SELECT to_jsonb(s)::text "
                "FROM yclients_projection_suppressions AS s "
                "WHERE external_id = '401'"
            )
        )
        assert set(stored_suppression) == {"external_id", "created_at"}
        assert stored_suppression["external_id"] == "401"

        starts_at = datetime.now(UTC)
        replacement = ProjectionSnapshot(
            records=(
                ProjectionRecord(
                    external_id="401",
                    booking_key=booking_key,
                    bot_marker_state="valid",
                    starts_at=starts_at,
                    scheduled_end_at=None,
                    status="confirmed",
                    deleted=False,
                    client_name="Удалённый клиент",
                    staff_name="Мастер",
                    service_names=("Услуга",),
                ),
                ProjectionRecord(
                    external_id="402",
                    booking_key=uuid4(),
                    bot_marker_state="valid",
                    starts_at=starts_at,
                    scheduled_end_at=None,
                    status="confirmed",
                    deleted=False,
                    client_name="Контроль",
                    staff_name="Мастер",
                    service_names=("Услуга",),
                ),
            ),
            synced_at=starts_at,
        )
        projection = ProjectionRepository(pool)
        async with projection.serialized() as projection_conn:
            assert projection_conn is not None
            await projection.replace(projection_conn, replacement)
        assert [
            row["external_id"]
            for row in await conn.fetch(
                "SELECT external_id FROM yclients_booking_projection "
                "ORDER BY external_id"
            )
        ] == ["402"]

        assert await cache.get("chat:42:messages") is None
        assert await cache.get("buffer:42") is None
        assert await cache.get("lock:buffer:42") is None
        assert await cache.zscore("buffer:deadlines", "42") is None
        assert await cache.zscore("buffer:deadlines", "84") == 1
        assert await cache.get("consent:state:telegram:42:7") is None
        assert await cache.get("bot:paused") == "1"

        audit = await conn.fetchrow(
            "SELECT object_id, before, after FROM admin_audit_events "
            "WHERE action = 'customer_data.delete' ORDER BY id DESC LIMIT 1"
        )
        assert audit["object_id"] is None
        assert audit["before"] is None
        payload = audit["after"] if isinstance(audit["after"], dict) else json.loads(audit["after"])
        assert payload["channel"] == "telegram"
        assert "chat_id" not in payload
        assert "user_id" not in payload
    finally:
        await cache.flushdb()
        await cache.aclose()
        await pool.close()
        await conn.close()


@pytest.mark.parametrize("failure_target", ["suppression", "projection"])
async def test_projection_privacy_failure_rolls_back_customer_deletion(
    migrated_database_url,
    failure_target,
):
    conn = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=2)
    await pool.connect()
    cache = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await cache.flushdb()
    scenario_id = uuid4()
    booking_key = uuid4()
    try:
        await conn.execute(
            """
            INSERT INTO booking_scenarios
                (id, kind, phase, idempotency_key, customer_id, state)
            VALUES ($1, 'create', 'confirmed', $2, '42', '{}')
            """,
            scenario_id,
            f"privacy-rollback:{scenario_id}",
        )
        await conn.execute(
            """
            INSERT INTO bookings
                (id, last_scenario_id, external_id, customer_id, slot_id,
                 starts_at, status, snapshot, booking_key)
            VALUES ($1, $2, 'privacy-rollback', '42', 'slot', now(),
                    'confirmed', '{}', $3)
            """,
            uuid4(),
            scenario_id,
            booking_key,
        )
        await conn.execute(
            """
            INSERT INTO yclients_booking_projection
                (external_id, booking_key, bot_marker_state, starts_at,
                 status, deleted, service_names, synced_at)
            VALUES ('privacy-rollback', $1, 'valid', now(),
                    'confirmed', false, ARRAY[]::text[], now())
            """,
            booking_key,
        )
        table, action = (
            ("yclients_projection_suppressions", "INSERT")
            if failure_target == "suppression"
            else ("yclients_booking_projection", "DELETE")
        )
        await conn.execute(
            f"""
            CREATE FUNCTION reject_projection_privacy_change() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'forced privacy failure';
            END;
            $$;
            CREATE TRIGGER reject_projection_privacy_change
            BEFORE {action} ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_projection_privacy_change();
            """
        )

        with pytest.raises(
            CustomerDataDeletionError,
            match="^customer data deletion failed$",
        ):
            await delete_customer_data(
                pool=pool,
                redis_client=cache,
                chat_id=42,
                actor_id=1,
                ip_address=None,
                user_agent=None,
            )

        assert await conn.fetchval(
            "SELECT count(*) FROM bookings WHERE customer_id='42'"
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM yclients_booking_projection "
            "WHERE external_id='privacy-rollback'"
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM yclients_projection_suppressions"
        ) == 0
    finally:
        await cache.flushdb()
        await cache.aclose()
        await pool.close()
        await conn.close()


async def test_deletion_waits_for_projection_writer(migrated_database_url):
    lock_conn = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=2)
    await pool.connect()
    cache = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    await lock_conn.execute(
        "SELECT pg_advisory_lock(hashtextextended($1, 0))",
        PROJECTION_LOCK,
    )
    task = asyncio.create_task(
        delete_customer_data(
            pool=pool,
            redis_client=cache,
            chat_id=42,
            actor_id=1,
            ip_address=None,
            user_agent=None,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert task.done() is False

        await lock_conn.execute(
            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
            PROJECTION_LOCK,
        )
        # This asserts lock ordering, not a 3-second deletion latency SLO.
        result = await asyncio.wait_for(task, timeout=10)

        assert result.status == "already_absent"
    finally:
        if not task.done():
            task.cancel()
        await lock_conn.execute(
            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
            PROJECTION_LOCK,
        )
        await cache.delete("privacy:deleting:telegram:42")
        await cache.aclose()
        await pool.close()
        await lock_conn.close()


async def test_redis_cleanup_failure_rolls_back_postgres(migrated_database_url):
    conn = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=2)
    await pool.connect()
    cache = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

    class BrokenPipeline:
        def delete(self, *_args):
            return self

        def zrem(self, *_args):
            return self

        async def execute(self):
            raise ConnectionError("private redis detail")

    class CleanupFailingRedis:
        async def set(self, *args, **kwargs):
            return await cache.set(*args, **kwargs)

        def lock(self, *args, **kwargs):
            return cache.lock(*args, **kwargs)

        def pipeline(self, **_kwargs):
            return BrokenPipeline()

        async def delete(self, *args):
            return await cache.delete(*args)

    try:
        await conn.execute(
            "INSERT INTO messages (chat_id, user_id, role, content) "
            "VALUES (42, 7, 'user', 'must remain')"
        )

        with pytest.raises(CustomerDataDeletionError) as error:
            await delete_customer_data(
                pool=pool,
                redis_client=CleanupFailingRedis(),
                chat_id=42,
                actor_id=1,
                ip_address=None,
                user_agent=None,
            )

        assert str(error.value) == "customer data deletion failed"
        assert await conn.fetchval("SELECT count(*) FROM messages WHERE chat_id = 42") == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM admin_audit_events "
            "WHERE action = 'customer_data.delete'"
        ) == 0
    finally:
        await cache.delete("privacy:deleting:telegram:42")
        await cache.aclose()
        await pool.close()
        await conn.close()


async def test_waits_for_existing_telegram_advisory_lock(migrated_database_url):
    lock_conn = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=2)
    await pool.connect()
    cache = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    transaction = lock_conn.transaction()
    await transaction.start()
    await lock_conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        "42",
    )
    task = asyncio.create_task(
        delete_customer_data(
            pool=pool,
            redis_client=cache,
            chat_id=42,
            actor_id=1,
            ip_address=None,
            user_agent=None,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert task.done() is False
        assert await cache.get("privacy:deleting:telegram:42") is not None

        await transaction.rollback()
        result = await asyncio.wait_for(task, timeout=3)

        assert result.status == "already_absent"
        assert await cache.get("privacy:deleting:telegram:42") is None
    finally:
        if not task.done():
            task.cancel()
        if lock_conn.is_in_transaction():
            await transaction.rollback()
        await cache.delete("privacy:deleting:telegram:42")
        await cache.aclose()
        await pool.close()
        await lock_conn.close()


async def test_inbox_accept_waits_for_customer_lock_and_rechecks_consent(
    migrated_database_url,
):
    lock_conn = await asyncpg.connect(migrated_database_url)
    database = Database(migrated_database_url, min_size=1, max_size=2)
    await database.connect()
    await lock_conn.execute(
        "INSERT INTO processing_consents (channel, user_id, consent_version) "
        "VALUES ('telegram', '7', 'v1')"
    )
    transaction = lock_conn.transaction()
    await transaction.start()
    await lock_conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        "42",
    )
    accepted = asyncio.create_task(
        MessageRepository(database).accept_if_consented(
            IncomingMessage(
                update_id="race-update",
                message_id="race-message",
                channel="telegram",
                chat_id="42",
                user_id="7",
                text="race secret",
                received_at=datetime.now(UTC),
                correlation_id=uuid4(),
            ),
            enqueue_directly=True,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert accepted.done() is False
        await lock_conn.execute(
            "DELETE FROM processing_consents "
            "WHERE channel = 'telegram' AND user_id = '7'"
        )
        await transaction.commit()

        assert await asyncio.wait_for(accepted, timeout=3) is False
        assert await lock_conn.fetchval("SELECT count(*) FROM message_inbox") == 0
        assert await lock_conn.fetchval("SELECT count(*) FROM task_outbox") == 0
    finally:
        if not accepted.done():
            accepted.cancel()
        if lock_conn.is_in_transaction():
            await transaction.rollback()
        await database.close()
        await lock_conn.close()


async def test_deletion_waits_for_real_buffer_lock(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=2)
    await database.connect()
    cache = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    buffer_lock = cache.lock(
        "lock:buffer:42", timeout=30, blocking_timeout=1
    )
    assert await buffer_lock.acquire()
    deletion = asyncio.create_task(
        delete_customer_data(
            pool=database,
            redis_client=cache,
            chat_id=42,
            actor_id=1,
            ip_address=None,
            user_agent=None,
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert deletion.done() is False
        await buffer_lock.release()

        result = await asyncio.wait_for(deletion, timeout=3)

        assert result.status == "already_absent"
    finally:
        if not deletion.done():
            deletion.cancel()
        try:
            if await buffer_lock.owned():
                await buffer_lock.release()
        except Exception:
            pass
        await cache.delete("privacy:deleting:telegram:42")
        await cache.aclose()
        await database.close()


async def test_parallel_delete_cannot_remove_active_marker(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=2)
    await database.connect()
    cache = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    privacy_lock = cache.lock(
        "lock:privacy-delete:telegram:42",
        timeout=300,
        blocking_timeout=1,
    )
    assert await privacy_lock.acquire()
    marker_token = "active-owner-token"
    await cache.set("privacy:deleting:telegram:42", marker_token, ex=300)
    try:
        with pytest.raises(CustomerDataDeletionError):
            await delete_customer_data(
                pool=database,
                redis_client=cache,
                chat_id=42,
                actor_id=1,
                ip_address=None,
                user_agent=None,
            )
        assert await cache.get("privacy:deleting:telegram:42") == marker_token
    finally:
        if await privacy_lock.owned():
            await privacy_lock.release()
        await cache.delete("privacy:deleting:telegram:42")
        await cache.aclose()
        await database.close()


async def test_scheduler_rechecks_job_after_customer_lock_before_provider_call(
    migrated_database_url,
):
    database = Database(migrated_database_url, min_size=1, max_size=4)
    await database.connect()
    conn = await asyncpg.connect(migrated_database_url)
    scenario_id = uuid4()
    booking_key = uuid4()
    job_id = uuid4()
    await conn.execute(
        "INSERT INTO booking_scenarios "
        "(id, kind, phase, idempotency_key, customer_id, state) "
        "VALUES ($1, 'create', 'confirmed', 'race-scenario', '42', '{}')",
        scenario_id,
    )
    await conn.execute(
        """
        INSERT INTO bookings
            (id, last_scenario_id, external_id, customer_id, slot_id,
             starts_at, status, snapshot, booking_key)
        VALUES ($1, $2, 'external-race', '42', 'slot', now(),
                'confirmed', '{}', $3)
        """,
        uuid4(), scenario_id, booking_key,
    )
    await conn.execute(
        """
        INSERT INTO scheduler_jobs
            (id, kind, run_at, payload, idempotency_key, status,
             booking_key, booking_starts_at)
        SELECT $1, 'no_show_check', now(), '{}', $2, 'claimed',
               booking_key, starts_at
        FROM bookings WHERE booking_key = $3
        """,
        job_id, f"scheduler-race:{job_id}", booking_key,
    )

    class MustNotCall:
        def __getattr__(self, name):
            raise AssertionError(f"provider-side effect attempted: {name}")

    handler = MessageTaskHandler(
        database,
        object(),
        object(),
        scheduler_repository=SchedulerJobRepository(database),
        booking_port=MustNotCall(),
        notification_outbox=MustNotCall(),
        lifecycle=MustNotCall(),
    )
    transaction = conn.transaction()
    await transaction.start()
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "42"
    )
    processing = asyncio.create_task(
        handler.handle(
            QueueTask(
                kind="scheduler_job",
                payload={"job_id": str(job_id)},
                idempotency_key=f"scheduler_job:{job_id}",
            )
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert processing.done() is False
        await conn.execute("DELETE FROM scheduler_jobs WHERE id = $1", job_id)
        await conn.execute("DELETE FROM bookings WHERE booking_key = $1", booking_key)
        await conn.execute("DELETE FROM booking_scenarios WHERE id = $1", scenario_id)
        await transaction.commit()

        await asyncio.wait_for(processing, timeout=3)
    finally:
        if not processing.done():
            processing.cancel()
        if conn.is_in_transaction():
            await transaction.rollback()
        await database.close()
        await conn.close()
