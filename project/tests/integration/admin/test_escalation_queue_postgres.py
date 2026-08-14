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


async def test_open_escalations_are_bounded_safe_and_newest_first(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=2)
    await pool.connect()
    previous_pool = admin_database._pool
    admin_database._pool = pool
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
        assert all("payload" not in row for row in result)
        assert all("reason_code" not in row for row in result)
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


async def test_resolve_escalation_is_atomic_idempotent_and_keeps_other_handoff(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=2)
    await pool.connect()
    previous_pool = admin_database._pool
    admin_database._pool = pool
    first_id = uuid4()
    second_id = uuid4()
    missing_id = uuid4()
    try:
        await connection.executemany(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload)
            VALUES ($1, 'feedback', '42', 'open', $2, $3::jsonb)
            """,
            [
                (first_id, "low_feedback_rating", '{"secret":"first"}'),
                (second_id, "private-reason", '{"secret":"second"}'),
            ],
        )
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id, enabled_at)
            VALUES ('42', true, 'private-reason', $1, now())
            """,
            second_id,
        )

        first = await admin_database.resolve_escalation(
            first_id,
            actor_id=7,
            ip_address="127.0.0.1",
            user_agent="test",
        )
        assert first == "resolved"
        assert await connection.fetchval(
            "SELECT status FROM escalations WHERE id=$1", first_id
        ) == "resolved"
        assert await connection.fetchval(
            "SELECT enabled FROM human_mode WHERE customer_id='42'"
        ) is True

        second = await admin_database.resolve_escalation(
            second_id,
            actor_id=7,
            ip_address=None,
            user_agent=None,
        )
        assert second == "resolved"
        assert await connection.fetchval(
            "SELECT enabled FROM human_mode WHERE customer_id='42'"
        ) is False
        assert await admin_database.resolve_escalation(
            second_id,
            actor_id=7,
            ip_address=None,
            user_agent=None,
        ) == "already_resolved"
        assert await admin_database.resolve_escalation(
            missing_id,
            actor_id=7,
            ip_address=None,
            user_agent=None,
        ) == "not_found"

        audit = await connection.fetch(
            """
            SELECT action, object_type, object_id, before, after
            FROM admin_audit_events
            WHERE action='escalation.resolve'
            ORDER BY id
            """
        )
        assert len(audit) == 2
        assert [row["object_id"] for row in audit] == [str(first_id), str(second_id)]
        assert all(row["object_type"] == "escalation" for row in audit)
        assert all(json.loads(row["before"]) == {"status": "open"} for row in audit)
        assert all(
            json.loads(row["after"]) == {"status": "resolved"} for row in audit
        )
        assert "customer_id" not in repr(audit)
        assert "secret" not in repr(audit)
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()


async def test_parallel_resolve_creates_one_audit(migrated_database_url):
    connection = await asyncpg.connect(migrated_database_url)
    first_pool = Database(migrated_database_url, min_size=1, max_size=1)
    second_pool = Database(migrated_database_url, min_size=1, max_size=1)
    await first_pool.connect()
    await second_pool.connect()
    escalation_id = uuid4()
    previous_pool = admin_database._pool
    try:
        await connection.execute(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload)
            VALUES ($1, 'feedback', 'parallel-customer', 'open', 'private', '{}')
            """,
            escalation_id,
        )
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id, enabled_at)
            VALUES ('parallel-customer', true, 'private', $1, now())
            """,
            escalation_id,
        )

        async def resolve(pool):
            admin_database._pool = pool
            return await admin_database.resolve_escalation(
                escalation_id, actor_id=7, ip_address=None, user_agent=None
            )

        results = await asyncio.gather(resolve(first_pool), resolve(second_pool))

        assert sorted(results) == ["already_resolved", "resolved"]
        assert await connection.fetchval(
            "SELECT count(*) FROM admin_audit_events WHERE action='escalation.resolve'"
        ) == 1
        assert await connection.fetchval(
            "SELECT enabled FROM human_mode WHERE customer_id='parallel-customer'"
        ) is False
    finally:
        admin_database._pool = previous_pool
        await first_pool.close()
        await second_pool.close()
        await connection.close()


async def test_audit_failure_rolls_back_resolve_and_human_mode(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=1)
    await pool.connect()
    previous_pool = admin_database._pool
    admin_database._pool = pool
    escalation_id = uuid4()
    try:
        await connection.execute(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload)
            VALUES ($1, 'raw-source', 'customer-sentinel', 'open',
                    'raw-reason', '{"raw":"payload-sentinel"}')
            """,
            escalation_id,
        )
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id, enabled_at)
            VALUES ('customer-sentinel', true, 'raw-reason', $1, now())
            """,
            escalation_id,
        )
        await connection.execute(
            """
            CREATE FUNCTION reject_escalation_audit() RETURNS trigger AS $$
            BEGIN
                IF NEW.action = 'escalation.resolve' THEN
                    RAISE EXCEPTION 'forced audit failure';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER reject_escalation_audit
            BEFORE INSERT ON admin_audit_events
            FOR EACH ROW EXECUTE FUNCTION reject_escalation_audit();
            """
        )

        with pytest.raises(Exception, match="forced audit failure"):
            await admin_database.resolve_escalation(
                escalation_id, actor_id=7, ip_address=None, user_agent=None
            )

        assert await connection.fetchval(
            "SELECT status FROM escalations WHERE id=$1", escalation_id
        ) == "open"
        assert await connection.fetchval(
            "SELECT enabled FROM human_mode WHERE customer_id='customer-sentinel'"
        ) is True
        assert await connection.fetchval(
            "SELECT count(*) FROM admin_audit_events WHERE action='escalation.resolve'"
        ) == 0
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()
