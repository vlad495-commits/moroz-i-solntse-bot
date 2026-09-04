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


async def test_cleanup_removes_expired_booking_interaction_payloads(database):
    async with database.acquire() as connection:
        now = await connection.fetchval("SELECT now()")
        old = now - timedelta(days=31)
        fresh = now - timedelta(days=29)
        for update_id, created_at in (("old-contact", old), ("fresh-contact", fresh)):
            await connection.execute(
                "INSERT INTO message_inbox "
                "(id, channel, external_message_id, chat_id, payload, "
                "correlation_id, status, created_at) "
                "VALUES (gen_random_uuid(), 'telegram', $1, '42', $2::jsonb, "
                "gen_random_uuid(), 'processed', $3)",
                update_id,
                '{"kind":"contact","data":{"phone_number":"+79991234567"}}',
                created_at,
            )
        for key, created_at in (("old-booking-reply", old), ("fresh-booking-reply", fresh)):
            await connection.execute(
                "INSERT INTO outbound_messages "
                "(id, channel, chat_id, text, delivery_options, idempotency_key, "
                "status, created_at) VALUES (gen_random_uuid(), 'telegram', '42', "
                "'booking reply', '{}'::jsonb, $1, 'sent', $2)",
                key,
                created_at,
            )

    await RetentionCleanupCoordinator(
        database, Scheduler(), retention_days=30
    ).run(retention_job(NOW))

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT array_agg(external_message_id ORDER BY external_message_id) "
            "FROM message_inbox"
        ) == ["fresh-contact"]
        assert await connection.fetchval(
            "SELECT array_agg(idempotency_key ORDER BY idempotency_key) "
            "FROM outbound_messages"
        ) == ["fresh-booking-reply"]


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


async def test_reactivation_retention_preserves_active_and_fresh_records(database):
    async with database.acquire() as connection:
        now = await connection.fetchval("SELECT now()")
        old = now - timedelta(days=31)
        fresh = now - timedelta(days=29)
        version_id = await connection.fetchval(
            """
            INSERT INTO reactivation_program_versions
                (id, version_number, status, inactivity_days, reminder_enabled,
                 cooldown_days, main_text, reminder_text, template_checksum)
            VALUES (gen_random_uuid(), 9911, 'retired', 90, false, 90,
                    'main', 'reminder', 'retention-version')
            RETURNING id
            """
        )
        old_event = await connection.fetchval(
            """
            INSERT INTO marketing_consent_events
                (id, channel, user_id, action, consent_version,
                 proof_text_hash, source, source_event_id, occurred_at, created_at)
            VALUES (gen_random_uuid(), 'telegram', 'retention-inactive', 'revoked',
                    'marketing-v1', NULL, 'telegram_command', 'old-revoke', $1, $1)
            RETURNING id
            """,
            old,
        )
        active_event = await connection.fetchval(
            """
            INSERT INTO marketing_consent_events
                (id, channel, user_id, action, consent_version,
                 proof_text_hash, source, source_event_id, occurred_at, created_at)
            VALUES (gen_random_uuid(), 'telegram', 'retention-active', 'granted',
                    'marketing-v1', 'proof-hash', 'telegram_command',
                    'old-grant', $1, $1)
            RETURNING id
            """,
            old,
        )
        await connection.execute(
            """
            INSERT INTO marketing_consents
                (id, channel, user_id, consent_version, active, granted_at,
                 revoked_at, source, proof_event_id, proof_text_hash, updated_at)
            VALUES (gen_random_uuid(), 'telegram', 'retention-inactive',
                    'marketing-v1', false, $1, $1, 'telegram_command', NULL, NULL, $1),
                   (gen_random_uuid(), 'telegram', 'retention-active',
                    'marketing-v1', true, $1, NULL, 'telegram_command', $2,
                    'proof-hash', $1)
            """,
            old,
            active_event,
        )
        await connection.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, identity_status, sync_status, updated_at)
            VALUES ('telegram', 'retention-inactive', 'unverified', 'never', $1),
                   ('telegram', 'retention-active', 'verified', 'current', $1),
                   ('telegram', 'retention-open', 'verified', 'current', $1)
            """,
            old,
        )
        old_journey = await connection.fetchval(
            """
            INSERT INTO reactivation_journeys
                (id, channel, user_id, program_version_id, status, close_reason,
                 activity_anchor_at, created_at, updated_at, closed_at)
            VALUES (gen_random_uuid(), 'telegram', 'retention-inactive', $1,
                    'closed', 'exhausted', $2, $2, $2, $2)
            RETURNING id
            """,
            version_id,
            old,
        )
        fresh_journey = await connection.fetchval(
            """
            INSERT INTO reactivation_journeys
                (id, channel, user_id, program_version_id, status, close_reason,
                 activity_anchor_at, created_at, updated_at, closed_at)
            VALUES (gen_random_uuid(), 'telegram', 'retention-fresh', $1,
                    'closed', 'exhausted', $2, $2, $2, $2)
            RETURNING id
            """,
            version_id,
            fresh,
        )
        open_journey = await connection.fetchval(
            """
            INSERT INTO reactivation_journeys
                (id, channel, user_id, program_version_id, status,
                 activity_anchor_at, created_at, updated_at)
            VALUES (gen_random_uuid(), 'telegram', 'retention-open', $1,
                    'active', $2, $2, $2)
            RETURNING id
            """,
            version_id,
            old,
        )

    await RetentionCleanupCoordinator(
        database, Scheduler(), retention_days=30
    ).run(retention_job(NOW))

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journeys WHERE id = $1", old_journey
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journeys WHERE id = $1", fresh_journey
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journeys WHERE id = $1", open_journey
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM customer_activity_projection "
            "WHERE user_id = 'retention-inactive'"
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM customer_activity_projection "
            "WHERE user_id IN ('retention-active', 'retention-open')"
        ) == 2
        assert await connection.fetchval(
            "SELECT count(*) FROM marketing_consent_events WHERE id = $1", old_event
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM marketing_consent_events WHERE id = $1", active_event
        ) == 1
