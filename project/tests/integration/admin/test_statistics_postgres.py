from datetime import date, datetime, UTC
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest

import database as admin_database
from moroz.common.db import Database
from stats_calculations import parse_statistics_period


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


async def _open_admin_pool(database_url):
    pool = Database(database_url, min_size=1, max_size=2)
    await pool.connect()
    previous = admin_database._pool
    admin_database._pool = pool
    return pool, previous


async def test_statistics_snapshot_uses_only_selected_period_and_real_events(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    inside = datetime(2026, 8, 10, 9, 0, tzinfo=UTC)
    outside = datetime(2026, 7, 10, 9, 0, tzinfo=UTC)
    try:
        await connection.executemany(
            """
            INSERT INTO messages
                (chat_id, user_id, role, content, created_at)
            VALUES ($1, $2, $3, 'safe', $4)
            """,
            [
                (1, 101, "user", inside),
                (1, 101, "assistant", inside),
                (2, 202, "user", inside),
                (9, 909, "user", outside),
            ],
        )
        await connection.executemany(
            """
            INSERT INTO outbound_messages
                (id, channel, chat_id, text, idempotency_key, status, created_at)
            VALUES ($1, 'telegram', $2, 'safe', $3, $4, $5)
            """,
            [
                (uuid4(), "1", "reply:1", "sent", inside),
                (uuid4(), "2", "reply:2", "sent", inside),
                (uuid4(), "2", "reply:3", "sent", inside),
                (uuid4(), "3", "reply:4", "sent", inside),
                (uuid4(), "3", f"admin_handoff_reply:{uuid4()}:{uuid4()}", "sent", inside),
                (uuid4(), "4", "reply:pending", "pending", inside),
                (uuid4(), "9", "reply:old", "sent", outside),
            ],
        )
        await connection.execute(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload, created_at)
            VALUES ($1, 'feedback', '2', 'open', 'safe', '{}', $2)
            """,
            uuid4(),
            inside,
        )
        await connection.executemany(
            """
            INSERT INTO token_usage
                (chat_id, user_id, prompt_tokens, completion_tokens,
                 cached_tokens, total_tokens, model, created_at)
            VALUES (1, 101, $1, $2, $3, $4, $5, $6)
            """,
            [
                (100, 50, 20, 150, "gpt-4.1-mini", inside),
                (999, 999, 0, 1998, "old-model", outside),
            ],
        )

        result = await admin_database.get_statistics_snapshot(
            parse_statistics_period(date(2026, 8, 1), date(2026, 8, 31))
        )

        assert result["users"] == 2
        assert result["messages"] == 3
        assert result["automatic_replies"] == 4
        assert result["automated_dialogues"] == 1
        assert result["escalations"] == 1
        assert result["llm_calls"] == 1
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50
        assert result["cached_tokens"] == 20
        assert result["usage_rows"] == [
            {
                "model": "gpt-4.1-mini",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cached_tokens": 20,
            }
        ]
        assert result["security_incidents"] is None
        assert result["security_incidents_reason"] == (
            "Нет данных: Security-инциденты ещё не сохраняются."
        )
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()


async def test_statistics_settings_round_trip(migrated_database_url):
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    try:
        assert await admin_database.get_statistics_settings() == {
            "minutes_per_dialogue": None,
            "hourly_rate_rub": None,
        }

        saved = await admin_database.save_statistics_settings(
            Decimal("15.5"), Decimal("600")
        )

        assert saved == {
            "minutes_per_dialogue": Decimal("15.50"),
            "hourly_rate_rub": Decimal("600.00"),
        }
        assert await admin_database.get_statistics_settings() == saved
    finally:
        admin_database._pool = previous_pool
        await pool.close()


async def test_statistics_settings_reject_nonpositive_values(migrated_database_url):
    pool, previous_pool = await _open_admin_pool(migrated_database_url)
    try:
        with pytest.raises(ValueError, match="statistics settings"):
            await admin_database.save_statistics_settings(
                Decimal("0"), Decimal("600")
            )
    finally:
        admin_database._pool = previous_pool
        await pool.close()
