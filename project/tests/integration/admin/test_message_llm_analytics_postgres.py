from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

import database as admin_database
from moroz.common.db import Database


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


async def test_chat_detail_exposes_three_states_and_grouped_usage(
    migrated_database_url,
):
    connection = await asyncpg.connect(migrated_database_url)
    pool = Database(migrated_database_url, min_size=1, max_size=1)
    await pool.connect()
    previous_pool = admin_database._pool
    admin_database._pool = pool
    base = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)
    try:
        old_id = await connection.fetchval(
            "INSERT INTO messages "
            "(chat_id, user_id, role, content, created_at) "
            "VALUES (42, 7, 'user', 'old', $1) RETURNING id",
            base,
        )
        await connection.fetchval(
            "INSERT INTO messages "
            "(chat_id, user_id, role, content, llm_usage_tracked, created_at) "
            "VALUES (42, 7, 'user', 'without-llm', true, $1) RETURNING id",
            base + timedelta(seconds=1),
        )
        used_id = await connection.fetchval(
            "INSERT INTO messages "
            "(chat_id, user_id, role, content, llm_usage_tracked, created_at) "
            "VALUES (42, 7, 'user', 'with-llm', true, $1) RETURNING id",
            base + timedelta(seconds=2),
        )
        await connection.execute(
            "INSERT INTO messages "
            "(chat_id, user_id, role, content, created_at) "
            "VALUES (42, 7, 'assistant', 'answer', $1)",
            base + timedelta(seconds=3),
        )
        await connection.executemany(
            "INSERT INTO token_usage "
            "(chat_id, user_id, source_message_id, purpose, prompt_tokens, "
            "completion_tokens, cached_tokens, total_tokens, model) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            [
                (42, 7, None, "legacy", 1, 1, 0, 2, "old-model"),
                (42, 7, used_id, "router", 3, 1, 0, 4, "router-model"),
                (42, 7, used_id, "router", 4, 1, 1, 5, "router-model"),
                (42, 7, used_id, "answer", 9, 4, 1, 13, "answer-model"),
            ],
        )

        detail = await admin_database.get_chat_detail(42)

        assert detail is not None
        users = [
            message for message in detail["messages"]
            if message["role"] == "user"
        ]
        assert [message["id"] for message in users][0] == old_id
        assert [message["llm_usage_state"] for message in users] == [
            "unavailable",
            "none",
            "used",
        ]
        assert users[0]["usage_groups"] == []
        assert users[1]["usage_groups"] == []
        assert users[2]["usage_groups"] == [
            {
                "purpose": "answer",
                "model": "answer-model",
                "llm_calls": 1,
                "prompt_tokens": 9,
                "completion_tokens": 4,
                "cached_tokens": 1,
                "total_tokens": 13,
            },
            {
                "purpose": "router",
                "model": "router-model",
                "llm_calls": 2,
                "prompt_tokens": 7,
                "completion_tokens": 2,
                "cached_tokens": 1,
                "total_tokens": 9,
            },
        ]
        assert detail["stats"]["llm_calls"] == 4
    finally:
        admin_database._pool = previous_pool
        await pool.close()
        await connection.close()
