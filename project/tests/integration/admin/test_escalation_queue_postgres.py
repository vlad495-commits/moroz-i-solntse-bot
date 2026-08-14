import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

import database as admin_database
from moroz.common.db import Database


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


async def _open_admin_pool(database_url):
    pool = Database(database_url, min_size=1, max_size=3)
    await pool.connect()
    previous = admin_database._pool
    admin_database._pool = pool
    return pool, previous


async def test_open_escalations_are_bounded_safe_and_newest_first(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    base = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
    try:
        await connection.executemany(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload,
                 created_at, resolved_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
            """,
            [
                (uuid4(), "feedback", "84", "open", "low_feedback_rating",
                 '{"secret":"must-not-leak"}', base, None),
                (uuid4(), "private-source", "42", "open", "private-reason",
                 '{"secret":"must-not-leak"}', base + timedelta(minutes=1), None),
                (uuid4(), "feedback", "21", "resolved", "low_feedback_rating",
                 '{}', base + timedelta(minutes=2), base + timedelta(minutes=3)),
            ],
        )
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, enabled_at)
            VALUES ('42', true, 'private-reason', $1)
            """,
            base + timedelta(minutes=1),
        )

        result = await admin_database.get_open_escalations(limit=100)

        assert [row["customer_id"] for row in result] == ["42", "84"]
        assert all("payload" not in row and "reason_code" not in row for row in result)
        assert result[0]["reason"] == "Требуется помощь администратора"
        assert result[0]["source"] == "Система"
        assert result[0]["human_mode_enabled"] is True
        assert "must-not-leak" not in repr(result)
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()


@pytest.mark.parametrize("limit", [0, 101])
async def test_open_escalations_reject_invalid_limit(limit):
    with pytest.raises(ValueError, match="open escalations limit"):
        await admin_database.get_open_escalations(limit=limit)


async def _seed_open_handoff(connection, escalation_id, customer_id="42"):
    await connection.execute(
        """
        INSERT INTO escalations
            (id, source, customer_id, status, reason_code, payload)
        VALUES ($1, 'feedback', $2, 'open', 'private', '{}')
        """,
        escalation_id,
        customer_id,
    )
    await connection.execute(
        """
        INSERT INTO human_mode
            (customer_id, enabled, reason_code, escalation_id, enabled_at)
        VALUES ($1, true, 'private', $2, now())
        """,
        customer_id,
        escalation_id,
    )


async def test_enqueue_reply_is_atomic_idempotent_and_audited(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    escalation_id = uuid4()
    reply_token = uuid4()
    try:
        await _seed_open_handoff(connection, escalation_id)

        first_status, first_id = await admin_database.enqueue_escalation_reply(
            escalation_id,
            reply_token=reply_token,
            text="Ответ администратора",
            actor_id=7,
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )
        second_status, second_id = await admin_database.enqueue_escalation_reply(
            escalation_id,
            reply_token=reply_token,
            text="Ответ администратора",
            actor_id=7,
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )

        outbound = await connection.fetchrow(
            "SELECT channel, chat_id, text, idempotency_key, status "
            "FROM outbound_messages"
        )
        task = await connection.fetchrow("SELECT kind, payload, status FROM task_outbox")
        audit = await connection.fetchrow(
            """
            SELECT actor_id, action, object_type, object_id, before, after,
                   ip_address, user_agent
            FROM admin_audit_events
            """
        )

        assert (first_status, second_status) == ("queued", "already_queued")
        assert first_id == second_id
        assert tuple(outbound.values()) == (
            "telegram", "42", "Ответ администратора",
            f"admin_handoff_reply:{escalation_id}:{reply_token}", "pending",
        )
        assert task["kind"] == "send_outbound"
        assert json.loads(task["payload"]) == {"outbound_id": str(first_id)}
        assert task["status"] == "pending"
        assert tuple(audit.values())[:5] == (
            7, "escalation.reply_queued", "escalation", str(escalation_id), None,
        )
        assert json.loads(audit["after"]) == {
            "outbound_id": str(first_id), "status": "queued",
        }
        assert tuple(audit.values())[6:] == ("127.0.0.1", "test-agent")
        assert "Ответ администратора" not in repr(audit)
        assert "customer_id" not in repr(audit)
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()


async def test_concurrent_duplicate_reply_creates_one_outbound_and_audit(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    escalation_id = uuid4()
    reply_token = uuid4()
    try:
        await _seed_open_handoff(connection, escalation_id)

        async def enqueue():
            return await admin_database.enqueue_escalation_reply(
                escalation_id,
                reply_token=reply_token,
                text="Один ответ",
                actor_id=7,
                ip_address=None,
                user_agent=None,
            )

        results = await asyncio.gather(enqueue(), enqueue())

        assert sorted(status for status, _ in results) == ["already_queued", "queued"]
        assert len({outbound_id for _, outbound_id in results}) == 1
        assert await connection.fetchval("SELECT count(*) FROM outbound_messages") == 1
        assert await connection.fetchval("SELECT count(*) FROM task_outbox") == 1
        assert await connection.fetchval("SELECT count(*) FROM admin_audit_events") == 1
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()


async def test_concurrent_distinct_reply_tokens_create_one_pending_reply(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    escalation_id = uuid4()
    try:
        await _seed_open_handoff(connection, escalation_id)

        async def enqueue(reply_token):
            return await admin_database.enqueue_escalation_reply(
                escalation_id,
                reply_token=reply_token,
                text="Один ответ",
                actor_id=7,
                ip_address=None,
                user_agent=None,
            )

        results = await asyncio.gather(enqueue(uuid4()), enqueue(uuid4()))

        assert sorted(status for status, _ in results) == ["already_queued", "queued"]
        assert len({outbound_id for _, outbound_id in results}) == 1
        assert await connection.fetchval("SELECT count(*) FROM outbound_messages") == 1
        assert await connection.fetchval("SELECT count(*) FROM task_outbox") == 1
        assert await connection.fetchval("SELECT count(*) FROM admin_audit_events") == 1
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()


async def test_exact_reply_retry_is_idempotent_after_delivery(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    escalation_id = uuid4()
    reply_token = uuid4()
    try:
        await _seed_open_handoff(connection, escalation_id)
        first = await admin_database.enqueue_escalation_reply(
            escalation_id,
            reply_token=reply_token,
            text="Ответ",
            actor_id=7,
            ip_address=None,
            user_agent=None,
        )
        await connection.execute("UPDATE outbound_messages SET status='sent'")
        await connection.execute(
            "UPDATE escalations SET status='resolved', resolved_at=now() WHERE id=$1",
            escalation_id,
        )
        await connection.execute(
            "UPDATE human_mode SET enabled=false WHERE customer_id='42'"
        )

        retry = await admin_database.enqueue_escalation_reply(
            escalation_id,
            reply_token=reply_token,
            text="Ответ",
            actor_id=7,
            ip_address=None,
            user_agent=None,
        )

        assert first[0] == "queued"
        assert retry == ("already_queued", first[1])
        assert await connection.fetchval("SELECT count(*) FROM outbound_messages") == 1
        assert await connection.fetchval("SELECT count(*) FROM admin_audit_events") == 1
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()


async def test_enqueue_reply_rejects_missing_or_inactive_handoff(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    escalation_id = uuid4()
    try:
        await connection.execute(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload)
            VALUES ($1, 'feedback', '42', 'resolved', 'private', '{}')
            """,
            escalation_id,
        )
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id, enabled_at)
            VALUES ('42', false, 'private', $1, now())
            """,
            escalation_id,
        )

        inactive = await admin_database.enqueue_escalation_reply(
            escalation_id, reply_token=uuid4(), text="Не отправлять",
            actor_id=7, ip_address=None, user_agent=None,
        )
        missing = await admin_database.enqueue_escalation_reply(
            uuid4(), reply_token=uuid4(), text="Не отправлять",
            actor_id=7, ip_address=None, user_agent=None,
        )

        assert inactive == ("inactive", None)
        assert missing == ("not_found", None)
        assert await connection.fetchval("SELECT count(*) FROM outbound_messages") == 0
        assert await connection.fetchval("SELECT count(*) FROM task_outbox") == 0
        assert await connection.fetchval("SELECT count(*) FROM admin_audit_events") == 0
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()


async def test_reply_queue_audit_failure_rolls_back_outbound_and_task(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    escalation_id = uuid4()
    try:
        await _seed_open_handoff(connection, escalation_id)
        await connection.execute(
            """
            CREATE FUNCTION reject_reply_queue_audit() RETURNS trigger AS $$
            BEGIN
                IF NEW.action = 'escalation.reply_queued' THEN
                    RAISE EXCEPTION 'forced reply audit failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER reject_reply_queue_audit
            BEFORE INSERT ON admin_audit_events
            FOR EACH ROW EXECUTE FUNCTION reject_reply_queue_audit();
            """
        )

        with pytest.raises(Exception, match="forced reply audit failure"):
            await admin_database.enqueue_escalation_reply(
                escalation_id, reply_token=uuid4(), text="Откатить",
                actor_id=7, ip_address=None, user_agent=None,
            )

        assert await connection.fetchval("SELECT count(*) FROM outbound_messages") == 0
        assert await connection.fetchval("SELECT count(*) FROM task_outbox") == 0
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()
