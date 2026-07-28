from datetime import timedelta
from uuid import uuid4

import asyncpg
import pytest

import database as admin_database
from moroz.common.db import Database


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


async def test_system_metrics_snapshot_counts_durable_runtime_state(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=1)
    await pool.connect()
    previous_pool = admin_database._pool
    admin_database._pool = pool
    try:
        await connection.executemany(
            """
            INSERT INTO message_inbox
                (id, channel, external_message_id, chat_id, payload,
                 status, correlation_id, created_at)
            VALUES ($1, 'telegram', $2, '42', '{}'::jsonb, $3, $4,
                    now() - $5::interval)
            """,
            [
                (uuid4(), "update-1", "accepted", uuid4(), timedelta(seconds=20)),
                (uuid4(), "update-2", "accepted", uuid4(), timedelta(seconds=10)),
                (uuid4(), "update-3", "processed", uuid4(), timedelta(seconds=5)),
            ],
        )
        await connection.executemany(
            """
            INSERT INTO task_outbox
                (id, kind, payload, idempotency_key, status, published_at)
            VALUES ($1, 'process_message', '{}'::jsonb, $2, $3,
                    CASE WHEN $3 = 'published' THEN now() END)
            """,
            [
                (uuid4(), "task-1", "pending"),
                (uuid4(), "task-2", "published"),
            ],
        )
        await connection.executemany(
            """
            INSERT INTO outbound_messages
                (id, channel, chat_id, text, idempotency_key, status)
            VALUES ($1, 'telegram', '42', 'safe', $2, $3)
            """,
            [
                (uuid4(), "outbound-1", "sent"),
                (uuid4(), "outbound-2", "delivery_unknown"),
            ],
        )
        await connection.executemany(
            """
            INSERT INTO scheduler_jobs
                (id, kind, run_at, payload, idempotency_key, status)
            VALUES ($1, 'reminder', now(), '{}'::jsonb, $2, $3)
            """,
            [
                (uuid4(), "job-1", "finished"),
                (uuid4(), "job-2", "failed"),
            ],
        )
        await connection.executemany(
            """
            INSERT INTO token_usage
                (chat_id, prompt_tokens, completion_tokens, cached_tokens,
                 total_tokens, model)
            VALUES (42, $1, $2, 0, $3, 'test-model')
            """,
            [(10, 5, 15), (20, 10, 30)],
        )
        await connection.executemany(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload)
            VALUES ($1, 'booking', 'customer', $2, 'safe_code', '{}'::jsonb)
            """,
            [
                (uuid4(), "open"),
                (uuid4(), "resolved"),
            ],
        )

        result = await admin_database.get_system_metrics_snapshot()
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()

    assert result["bot_inbound_messages_total"] == 3
    assert result["worker_processed_messages_total"] == 1
    assert result["inbox_accepted_messages"] == 2
    assert result["inbox_oldest_age_seconds"] >= 20
    assert result["task_outbox_pending_messages"] == 1
    assert result["task_outbox_published_total"] == 1
    assert result["outbound_messages"] == {
        "delivery_unknown": 1,
        "sent": 1,
    }
    assert result["scheduler_jobs"] == {"failed": 1, "finished": 1}
    assert result["retained_llm_calls"] == 2
    assert result["retained_llm_tokens"] == 45
    assert result["open_escalations"] == 1
