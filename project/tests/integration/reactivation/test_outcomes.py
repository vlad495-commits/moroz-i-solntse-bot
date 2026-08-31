from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.reactivation.policy import ProgramPolicy, template_checksum
from moroz.reactivation.repository import ReactivationRepository


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio

SENT_AT = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def outcome_harness(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=2)
    await database.connect()
    policy = ProgramPolicy()
    version_id = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO reactivation_program_versions
                (id, version_number, status, inactivity_days, reminder_enabled,
                 reminder_after_days, cooldown_days, main_text, reminder_text,
                 template_checksum)
            VALUES ($1, 1, 'active', 90, true, 5, 90, $2, $3, $4)
            """,
            version_id,
            policy.main_text,
            policy.reminder_text,
            template_checksum(policy),
        )

    async def seed(user_id: str, event: str, occurred_at: datetime):
        journey_id = uuid4()
        client_id = str(1000 + int(user_id))
        async with database.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO customer_activity_projection
                    (channel, user_id, yclients_client_id, identity_status,
                     identity_source, identity_verified_at,
                     last_completed_visit_at, last_meaningful_inbound_at,
                     history_synced_at, recent_bookings_synced_at,
                     source_version, sync_status, updated_at)
                VALUES ('telegram', $1, $2, 'verified', 'test', $3,
                        $4, $5, $3, $3, 'test-v1', 'current', $3)
                """,
                user_id,
                client_id,
                SENT_AT,
                occurred_at if event == "completed" else SENT_AT - timedelta(days=90),
                occurred_at if event == "reply" else None,
            )
            await connection.execute(
                """
                INSERT INTO reactivation_journeys
                    (id, channel, user_id, program_version_id, status,
                     activity_anchor_at, first_sent_at, created_at, updated_at)
                VALUES ($1, 'telegram', $2, $3, 'active', $4, $5, $5, $5)
                """,
                journey_id,
                user_id,
                version_id,
                SENT_AT - timedelta(days=90),
                SENT_AT,
            )
            await connection.execute(
                """
                INSERT INTO reactivation_journey_steps
                    (id, journey_id, step_kind, status, due_at, sent_at,
                     idempotency_key, created_at, updated_at)
                VALUES ($1, $2, 'main', 'sent', $3, $3, $4, $3, $3)
                """,
                uuid4(),
                journey_id,
                SENT_AT,
                f"main:{journey_id}",
            )
            if event in {"booking", "completed"}:
                await connection.execute(
                    """
                    INSERT INTO yclients_booking_projection
                        (external_id, bot_marker_state, starts_at, status,
                         deleted, service_names, synced_at, client_id,
                         record_created_at)
                    VALUES ($1, 'absent', $2, $3, false, '{}'::text[],
                            $4, $5, $6)
                    """,
                    f"record-{journey_id}",
                    occurred_at,
                    "completed" if event == "completed" else "confirmed",
                    occurred_at,
                    client_id,
                    (
                        SENT_AT + timedelta(hours=1)
                        if event == "completed"
                        else occurred_at
                    ),
                )
        return journey_id

    try:
        yield database, ReactivationRepository(database), seed
    finally:
        await database.close()


@pytest.mark.parametrize(
    ("event", "offset", "field", "counted"),
    [
        ("reply", timedelta(days=7), "replied_at", True),
        ("reply", timedelta(days=7, seconds=1), "replied_at", False),
        ("booking", timedelta(days=14), "booked_at", True),
        ("booking", timedelta(days=14, seconds=1), "booked_at", False),
        ("completed", timedelta(days=30), "completed_visit_at", True),
        ("completed", timedelta(days=30, seconds=1), "completed_visit_at", False),
    ],
)
async def test_outcome_windows(outcome_harness, event, offset, field, counted):
    database, repository, seed = outcome_harness
    journey_id = await seed(str(int(offset.total_seconds())), event, SENT_AT + offset)

    await repository.refresh_outcomes(SENT_AT + timedelta(days=31))

    async with database.acquire() as connection:
        value = await connection.fetchval(
            f"SELECT {field} FROM reactivation_journeys WHERE id = $1",
            journey_id,
        )
    assert (value is not None) is counted


async def test_booking_requires_creation_after_send_and_refresh_is_idempotent(
    outcome_harness,
):
    database, repository, seed = outcome_harness
    journey_id = await seed("77", "booking", SENT_AT + timedelta(days=2))
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE yclients_booking_projection SET record_created_at = $1",
            SENT_AT - timedelta(seconds=1),
        )

    assert await repository.refresh_outcomes(SENT_AT + timedelta(days=3)) == 1
    assert await repository.refresh_outcomes(SENT_AT + timedelta(days=3)) == 1

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT booked_at, first_sent_at FROM reactivation_journeys WHERE id = $1",
            journey_id,
        )
    assert row["booked_at"] is None
    assert row["first_sent_at"] == SENT_AT


async def test_funnel_uses_actual_main_sends_and_separates_operational_failures(
    outcome_harness,
):
    database, repository, seed = outcome_harness
    answered_id = await seed("81", "reply", SENT_AT + timedelta(days=1))
    booked_id = await seed("82", "booking", SENT_AT + timedelta(days=2))
    completed_id = await seed("83", "completed", SENT_AT + timedelta(days=3))
    failed_id = await seed("84", "reply", SENT_AT + timedelta(days=8))
    unknown_id = await seed("85", "reply", SENT_AT + timedelta(days=8))
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE reactivation_journeys SET status = 'closed', close_reason = 'failed' "
            "WHERE id = $1",
            failed_id,
        )
        await connection.execute(
            "UPDATE reactivation_journey_steps SET status = 'failed' WHERE journey_id = $1",
            failed_id,
        )
        await connection.execute(
            "UPDATE reactivation_journeys SET status = 'closed', "
            "close_reason = 'delivery_unknown' WHERE id = $1",
            unknown_id,
        )
        await connection.execute(
            "UPDATE reactivation_journey_steps SET status = 'delivery_unknown' "
            "WHERE journey_id = $1",
            unknown_id,
        )
        await connection.execute(
            "UPDATE reactivation_journeys SET first_sent_at = NULL WHERE id = $1",
            failed_id,
        )

    await repository.refresh_outcomes(SENT_AT + timedelta(days=4))
    funnel = await repository.get_outcome_funnel(
        SENT_AT + timedelta(days=4), period_days=30
    )

    assert funnel.journey_started == 5
    assert funnel.main_sent == 4
    assert funnel.replied_7d == 1
    assert funnel.booked_14d == 2
    assert funnel.completed_30d == 1
    assert funnel.failed == 1
    assert funnel.delivery_unknown == 1
    assert answered_id and booked_id and completed_id
