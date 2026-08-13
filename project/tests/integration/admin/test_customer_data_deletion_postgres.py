import asyncio
import json
import os
from uuid import uuid4

import asyncpg
import pytest
import redis.asyncio as redis

from customer_data_deletion import CustomerDataDeletionError, delete_customer_data
from moroz.common.db import Database


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
    try:
        await conn.execute(
            "INSERT INTO messages (chat_id, user_id, username, role, content) "
            "VALUES (42, 7, 'target-user', 'user', 'secret'), "
            "(84, 8, 'control-user', 'user', 'keep')"
        )
        await conn.execute(
            "INSERT INTO token_usage (chat_id, user_id, model) "
            "VALUES (42, 7, 'test'), (84, 8, 'test')"
        )
        await conn.execute(
            "INSERT INTO processing_consents (channel, user_id, consent_version) "
            "VALUES ('telegram', '7', 'v1'), ('telegram', '8', 'v1')"
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
                   ($2, 'telegram', '84', 'keep reply', 'control-outbound')
            """,
            outbound_id, uuid4(),
        )
        await conn.execute(
            """
            INSERT INTO task_outbox (id, kind, payload, idempotency_key)
            VALUES ($1, 'process_message', '{"chat_id":"42"}'::jsonb, 'target-task'),
                   ($2, 'send_outbound', $3::jsonb, 'target-send'),
                   ($4, 'process_message', '{"chat_id":"84"}'::jsonb, 'control-task')
            """,
            uuid4(), uuid4(), json.dumps({"outbound_id": str(outbound_id)}), uuid4(),
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
            VALUES ($1, $2, 'external-secret', '42', 'slot', now(),
                    'confirmed', '{"phone":"secret"}'::jsonb, $3)
            """,
            booking_id, scenario_id, booking_key,
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
        await cache.set("lock:buffer:42", "secret")
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
        assert await conn.fetchval("SELECT count(*) FROM messages WHERE chat_id = 84") == 1
        assert await conn.fetchval("SELECT count(*) FROM processing_consents WHERE user_id = '8'") == 1

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
        "telegram:42",
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
        assert await cache.get("privacy:deleting:telegram:42") == "1"

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
