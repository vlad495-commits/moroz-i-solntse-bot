from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.retention import (
    RetentionCleanupCoordinator,
    RetentionCleanupError,
    retention_job,
)


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 18, 19, 22, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


class Scheduler:
    async def schedule(self, _job):
        return True


async def _seed(connection, chat_id, created_at):
    source_message_id = await connection.fetchval(
        "INSERT INTO messages "
        "(chat_id, user_id, role, content, llm_usage_tracked, created_at) "
        "VALUES ($1, $1, 'user', 'retention-test', true, $2) RETURNING id",
        chat_id,
        created_at,
    )
    await connection.execute(
        "INSERT INTO token_usage "
        "(chat_id, user_id, source_message_id, prompt_tokens, completion_tokens, "
        "cached_tokens, total_tokens, model, created_at) "
        "VALUES ($1, $1, $2, 1, 1, 0, 2, 'retention-test', $3)",
        chat_id,
        source_message_id,
        created_at,
    )
    return source_message_id


async def test_cleanup_deletes_expired_and_preserves_fresh_rows(database):
    async with database.acquire() as connection:
        database_now = await connection.fetchval("SELECT now()")
        expired_message_id = await _seed(
            connection, 8101, database_now - timedelta(days=1096)
        )
        fresh_message_id = await _seed(
            connection, 8102, database_now - timedelta(days=1094)
        )

    coordinator = RetentionCleanupCoordinator(
        database, Scheduler(), retention_days=1095
    )
    await coordinator.run(retention_job(NOW))

    async with database.acquire() as connection:
        rows = await connection.fetch(
            "SELECT chat_id, 'messages' AS source FROM messages "
            "WHERE chat_id IN (8101, 8102) UNION ALL "
            "SELECT chat_id, 'token_usage' AS source FROM token_usage "
            "WHERE chat_id IN (8101, 8102) ORDER BY source, chat_id"
        )
        assert await connection.fetchval(
            "SELECT count(*) FROM messages WHERE id = $1", expired_message_id
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM token_usage WHERE source_message_id = $1",
            expired_message_id,
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM messages WHERE id = $1", fresh_message_id
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM token_usage WHERE source_message_id = $1",
            fresh_message_id,
        ) == 1

    assert [(row["chat_id"], row["source"]) for row in rows] == [
        (8102, "messages"),
        (8102, "token_usage"),
    ]


async def test_second_delete_failure_rolls_back_first_delete(database):
    async with database.acquire() as connection:
        database_now = await connection.fetchval("SELECT now()")
        await _seed(connection, 8201, database_now - timedelta(days=1096))
        await connection.execute(
            "CREATE FUNCTION retention_test_fail() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'forced'; END $$"
        )
        await connection.execute(
            "CREATE TRIGGER retention_test_fail BEFORE DELETE ON token_usage "
            "FOR EACH STATEMENT EXECUTE FUNCTION retention_test_fail()"
        )

    coordinator = RetentionCleanupCoordinator(
        database, Scheduler(), retention_days=1095
    )
    with pytest.raises(RetentionCleanupError, match="^retention_cleanup_failed$"):
        await coordinator.run(retention_job(NOW))

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM messages WHERE chat_id = 8201"
        ) == 1
