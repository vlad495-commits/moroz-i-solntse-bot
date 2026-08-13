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
