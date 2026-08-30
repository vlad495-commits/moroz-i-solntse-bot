import os
import subprocess
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio

CONFIG = "/workspace/alembic.ini"
BASE_REVISION = "0022_admin_statistics"
HEAD_REVISION = "0023_reactivation_v2"

ACTIVITY_COLUMNS = (
    "channel", "user_id", "yclients_client_id", "identity_status",
    "identity_source", "identity_verified_at", "last_completed_visit_at",
    "last_meaningful_inbound_at", "next_active_booking_at",
    "history_synced_at", "recent_bookings_synced_at", "source_version",
    "sync_status", "sync_error_code", "created_at", "updated_at",
)
CONSENT_EVENT_COLUMNS = (
    "id", "channel", "user_id", "action", "consent_version",
    "proof_text_hash", "source", "source_event_id", "occurred_at", "created_at",
)
PROGRAM_VERSION_COLUMNS = (
    "id", "version_number", "status", "inactivity_days", "reminder_enabled",
    "reminder_after_days", "cooldown_days", "main_text", "reminder_text",
    "template_checksum", "created_by", "created_at", "activated_by",
    "activated_at", "preview_created_at", "preview_checksum",
    "preview_counts", "preview_population_watermark",
    "preview_history_watermark", "preview_recent_watermark",
    "test_outbound_id", "test_sent_at",
)
JOURNEY_COLUMNS = (
    "id", "channel", "user_id", "program_version_id", "status",
    "close_reason", "activity_anchor_at", "first_sent_at", "replied_at",
    "booked_at", "completed_visit_at", "escalated_at", "created_at",
    "updated_at", "closed_at",
)
STEP_COLUMNS = (
    "id", "journey_id", "step_kind", "status", "due_at", "reserved_at",
    "sent_at", "outbound_id", "idempotency_key", "terminal_reason",
    "created_at", "updated_at",
)


def run_alembic(database_url: str, *args: str) -> None:
    subprocess.run(
        ["alembic", "-c", CONFIG, *args],
        check=True,
        env={**os.environ, "DATABASE_URL": database_url},
    )


async def _column_names(connection: asyncpg.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        row["column_name"]
        for row in await connection.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            ORDER BY ordinal_position
            """,
            table,
        )
    )


async def test_reactivation_v2_schema_upgrade_constraints_and_downgrade(
    disposable_database_url,
) -> None:
    run_alembic(disposable_database_url, "upgrade", BASE_REVISION)
    connection = await asyncpg.connect(disposable_database_url)
    legacy_consent_id = uuid4()
    try:
        await connection.execute(
            """
            INSERT INTO marketing_consents
                (id, channel, user_id, consent_version, active, granted_at, created_at, updated_at)
            VALUES ($1, 'telegram', 'legacy-user', 'legacy-v1', true, now(), now(), now())
            """,
            legacy_consent_id,
        )
    finally:
        await connection.close()

    run_alembic(disposable_database_url, "upgrade", "head")
    connection = await asyncpg.connect(disposable_database_url)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    program_id = uuid4()
    journey_id = uuid4()
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == HEAD_REVISION
        assert await _column_names(connection, "customer_activity_projection") == ACTIVITY_COLUMNS
        assert await _column_names(connection, "marketing_consent_events") == CONSENT_EVENT_COLUMNS
        assert await _column_names(connection, "reactivation_program_versions") == PROGRAM_VERSION_COLUMNS
        assert await _column_names(connection, "reactivation_journeys") == JOURNEY_COLUMNS
        assert await _column_names(connection, "reactivation_journey_steps") == STEP_COLUMNS

        indexes = {
            row["indexname"]: row["indexdef"]
            for row in await connection.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public'"
            )
        }
        for name in (
            "uq_customer_activity_projection_verified_yclients_client",
            "uq_marketing_consent_events_source_event",
            "uq_reactivation_program_versions_number",
            "uq_reactivation_program_versions_active",
            "uq_reactivation_journeys_open_customer",
            "uq_reactivation_journey_steps_journey_kind",
            "uq_reactivation_journey_steps_idempotency_key",
            "uq_reactivation_journey_steps_outbound_id",
        ):
            assert name in indexes

        constraints = {
            row["conname"]: row["definition"]
            for row in await connection.fetch(
                """
                SELECT conname, pg_get_constraintdef(oid, true) AS definition
                FROM pg_constraint
                WHERE connamespace = 'public'::regnamespace
                """
            )
        }
        assert "FOREIGN KEY (proof_event_id) REFERENCES marketing_consent_events(id) ON DELETE SET NULL" in constraints["fk_marketing_consents_proof_event"]
        assert "FOREIGN KEY (program_version_id) REFERENCES reactivation_program_versions(id) ON DELETE RESTRICT" in constraints["fk_reactivation_journeys_program_version"]
        assert "FOREIGN KEY (journey_id) REFERENCES reactivation_journeys(id) ON DELETE CASCADE" in constraints["fk_reactivation_journey_steps_journey"]

        legacy = await connection.fetchrow(
            "SELECT source, proof_event_id, active FROM marketing_consents WHERE id = $1",
            legacy_consent_id,
        )
        assert tuple(legacy) == ("legacy_unproven", None, False)

        await connection.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, yclients_client_id, identity_status, sync_status, created_at, updated_at)
            VALUES ('telegram', '42', 'yclients-42', 'verified', 'current', $1, $1)
            """,
            now,
        )
        await connection.execute(
            """
            INSERT INTO marketing_consent_events
                (id, channel, user_id, action, consent_version, proof_text_hash,
                 source, source_event_id, occurred_at, created_at)
            VALUES ($1, 'telegram', '42', 'granted', 'v2', 'hash',
                    'telegram_update', 'event-42', $2, $2)
            """,
            uuid4(),
            now,
        )
        await connection.execute(
            """
            INSERT INTO reactivation_program_versions
                (id, version_number, status, inactivity_days, reminder_enabled,
                 reminder_after_days, cooldown_days, main_text, reminder_text,
                 template_checksum, created_at)
            VALUES ($1, 1, 'draft', 90, true, 5, 90, 'Основное', 'Напоминание', 'checksum', $2)
            """,
            program_id,
            now,
        )
        await connection.execute(
            """
            INSERT INTO reactivation_journeys
                (id, channel, user_id, program_version_id, status, activity_anchor_at, created_at, updated_at)
            VALUES ($1, 'telegram', '42', $2, 'scheduled', $3, $3, $3)
            """,
            journey_id,
            program_id,
            now,
        )
        await connection.execute(
            """
            INSERT INTO reactivation_journey_steps
                (id, journey_id, step_kind, status, due_at, idempotency_key, created_at, updated_at)
            VALUES ($1, $2, 'main', 'scheduled', $3, 'reactivation-v2-42-main', $3, $3)
            """,
            uuid4(),
            journey_id,
            now,
        )

        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO customer_activity_projection (channel, user_id, identity_status, sync_status, created_at, updated_at) VALUES ('telegram', 'invalid-activity', 'wrong', 'current', $1, $1)",
                now,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO marketing_consent_events (id, channel, user_id, action, source, source_event_id, occurred_at, created_at) VALUES ($1, 'telegram', 'invalid-consent', 'wrong', 'manual', 'invalid', $2, $2)",
                uuid4(),
                now,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO reactivation_program_versions (id, version_number, status, inactivity_days, reminder_enabled, cooldown_days, main_text, reminder_text, template_checksum, created_at) VALUES ($1, 2, 'draft', 30, false, 30, 'Основное', 'Напоминание', 'checksum-2', $2)",
                uuid4(),
                now,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO reactivation_journeys (id, channel, user_id, program_version_id, status, activity_anchor_at, created_at, updated_at) VALUES ($1, 'telegram', 'invalid-journey', $2, 'wrong', $3, $3, $3)",
                uuid4(),
                program_id,
                now,
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await connection.execute(
                "INSERT INTO reactivation_journey_steps (id, journey_id, step_kind, status, due_at, idempotency_key, created_at, updated_at) VALUES ($1, $2, 'reminder', 'wrong', $3, 'reactivation-v2-invalid', $3, $3)",
                uuid4(),
                journey_id,
                now,
            )
    finally:
        await connection.close()

    run_alembic(disposable_database_url, "downgrade", BASE_REVISION)
    connection = await asyncpg.connect(disposable_database_url)
    try:
        assert await connection.fetchval("SELECT version_num FROM alembic_version") == BASE_REVISION
        for table in (
            "marketing_consents",
            "reactivation_settings",
            "reactivation_campaigns",
            "reactivation_deliveries",
            "yclients_booking_projection",
            "admin_users",
            "outbound_messages",
        ):
            assert await connection.fetchval("SELECT to_regclass($1)", f"public.{table}") == table
        assert await connection.fetchval(
            "SELECT user_id FROM marketing_consents WHERE id = $1", legacy_consent_id
        ) == "legacy-user"
        for table in (
            "customer_activity_projection",
            "marketing_consent_events",
            "reactivation_program_versions",
            "reactivation_journeys",
            "reactivation_journey_steps",
        ):
            assert await connection.fetchval("SELECT to_regclass($1)", f"public.{table}") is None
    finally:
        await connection.close()
