from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest

import database as admin_database
from moroz.common.db import Database


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


async def test_customer_events_merge_sources_paginate_and_isolate_customer(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=2)
    await pool.connect()
    previous_pool = admin_database._pool
    admin_database._pool = pool
    base = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
    scenario_id = uuid4()
    booking_key = uuid4()
    escalation_id = uuid4()
    try:
        await connection.execute(
            """
            INSERT INTO messages
                (chat_id, user_id, role, content, created_at)
            VALUES
                (42, 7, 'user', '<script>target</script>', $1),
                (84, 8, 'user', 'control-secret', $2)
            """,
            base,
            base + timedelta(hours=1),
        )
        await connection.execute(
            """
            INSERT INTO booking_scenarios
                (id, kind, phase, idempotency_key, customer_id, state,
                 created_at, updated_at)
            VALUES ($1, 'create', 'confirmed', 'journal-scenario', '42',
                    '{"phone":"must-not-leak"}'::jsonb, $2, $2)
            """,
            scenario_id,
            base + timedelta(minutes=1),
        )
        await connection.execute(
            """
            INSERT INTO booking_events
                (id, scenario_id, event_type, payload, created_at)
            VALUES ($1, $2, 'booking_confirmed',
                    '{"phone":"must-not-leak"}'::jsonb, $3)
            """,
            uuid4(),
            scenario_id,
            base + timedelta(minutes=1),
        )
        await connection.execute(
            """
            INSERT INTO bookings
                (id, last_scenario_id, external_id, customer_id, slot_id,
                 starts_at, status, snapshot, booking_key)
            VALUES ($1, $2, 'journal-external', '42', 'slot', $3,
                    'confirmed', '{"phone":"must-not-leak"}'::jsonb, $4)
            """,
            uuid4(),
            scenario_id,
            base + timedelta(days=1),
            booking_key,
        )
        await connection.execute(
            """
            INSERT INTO scheduler_jobs
                (id, kind, run_at, payload, idempotency_key, status,
                 booking_key, created_at, updated_at, finished_at)
            VALUES ($1, 'reminder', $2,
                    '{"token":"must-not-leak"}'::jsonb,
                    'journal-job', 'finished', $3, $4, $5, $5)
            """,
            uuid4(),
            base + timedelta(minutes=4),
            booking_key,
            base + timedelta(minutes=2),
            base + timedelta(minutes=5),
        )
        await connection.execute(
            """
            INSERT INTO escalations
                (id, source, customer_id, booking_key, status, reason_code,
                 payload, created_at, resolved_at)
            VALUES ($1, 'feedback', '42', $2, 'resolved',
                    'low_feedback_rating', '{"phone":"must-not-leak"}',
                    $3, $4)
            """,
            escalation_id,
            booking_key,
            base + timedelta(minutes=3),
            base + timedelta(minutes=6),
        )
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, escalation_id, enabled_at)
            VALUES ('42', true, 'low_feedback_rating', $1, $2)
            """,
            escalation_id,
            base + timedelta(minutes=4),
        )
        await connection.execute(
            """
            INSERT INTO admin_audit_events
                (action, object_type, object_id, before, after, created_at)
            VALUES
                ('customer.note', 'customer', '42', NULL,
                 '{"phone":"must-not-leak"}'::jsonb, $1),
                ('customer_data.delete', 'customer_data', NULL, NULL,
                 '{"deleted":1}'::jsonb, $1)
            """,
            base + timedelta(minutes=7),
        )
        await connection.execute(
            """
            INSERT INTO scheduler_jobs
                (id, kind, run_at, payload, idempotency_key, status,
                 booking_key, created_at, updated_at)
            VALUES ($1, 'conflicting-secret', $2,
                    '{"customer_id":"84"}'::jsonb,
                    'conflicting-owner-job', 'pending', $3, $2, $2)
            """,
            uuid4(),
            base + timedelta(minutes=8),
            booking_key,
        )

        first = await admin_database.get_customer_events(42, limit=3)
        await connection.execute(
            "INSERT INTO messages (chat_id, user_id, role, content, created_at) "
            "VALUES (42, 7, 'user', 'new-after-first-page', $1)",
            base + timedelta(minutes=5, seconds=30),
        )
        second = await admin_database.get_customer_events(
            42,
            limit=3,
            cursor=first["next_cursor"],
        )
        assert [event["title"] for event in first["items"]] == [
            "Заметка администратора",
            "Обращение администратора закрыто",
            "Уведомление отправлено",
        ]
        assert first["has_more"] is True
        assert first["next_cursor"]
        assert {item["event_id"] for item in first["items"]}.isdisjoint(
            item["event_id"] for item in second["items"]
        )
        all_visible = first["items"] + second["items"]
        assert "control-secret" not in repr(all_visible)
        assert "must-not-leak" not in repr(all_visible)
        assert all("payload" not in event for event in all_visible)
        assert "customer_data.delete" not in repr(all_visible)
        assert "conflicting-secret" not in repr(first)
        assert "new-after-first-page" not in repr(second)
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()


@pytest.mark.parametrize(
    "limit",
    [0, 51],
)
async def test_customer_events_reject_invalid_page_bounds(limit):
    with pytest.raises(ValueError, match="customer events page bounds"):
        await admin_database.get_customer_events(42, limit=limit)


async def test_customer_events_reject_malformed_cursor():
    with pytest.raises(ValueError, match="customer events cursor"):
        await admin_database.get_customer_events(42, cursor="not-a-cursor")
