import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.reactivation.policy import ProgramPolicy, next_send_at, template_checksum
from moroz.reactivation.repository import ReactivationRepository


NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def planner(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=5)
    await database.connect()
    version_id = uuid4()
    policy = ProgramPolicy()
    async with database.acquire() as connection:
        owner_id = await connection.fetchval(
            """
            INSERT INTO admin_users
                (username, role, password_hash, totp_secret, enabled)
            VALUES ('planner-owner', 'owner', 'x', 'x', true)
            RETURNING id
            """
        )
        await connection.execute(
            """
            INSERT INTO reactivation_program_versions
                (id, version_number, status, inactivity_days, reminder_enabled,
                 reminder_after_days, cooldown_days, main_text, reminder_text,
                 template_checksum)
            VALUES ($1, 1, 'active', 90, true, 5, 90, $2, $3, $4)
            """,
            version_id, policy.main_text, policy.reminder_text,
            template_checksum(policy),
        )
        await connection.execute(
            """
            UPDATE reactivation_settings
            SET mode = 'active', active_version_id = $1,
                legal_status = 'approved', legal_reference = 'review',
                legal_approved_at = $2, legal_approved_by = $3,
                program_revision = 1
            WHERE id = 1
            """,
            version_id, NOW, owner_id,
        )
    try:
        yield database, ReactivationRepository(database), version_id
    finally:
        await database.close()


async def _seed_eligible(connection, user_id: str, *, now: datetime = NOW):
    event_id = uuid4()
    await connection.execute(
        """
        INSERT INTO marketing_consent_events
            (id, channel, user_id, action, consent_version, proof_text_hash,
             source, source_event_id, occurred_at)
        VALUES ($1, 'telegram', $2, 'granted', 'v1', 'proof', 'telegram', $3, $4)
        """,
        event_id, user_id, f"grant-{user_id}", now - timedelta(days=200),
    )
    await connection.execute(
        """
        INSERT INTO marketing_consents
            (id, channel, user_id, active, consent_version, granted_at, source,
             proof_event_id, proof_text_hash, updated_at)
        VALUES ($1, 'telegram', $2, true, 'v1', $3, 'telegram', $4, 'proof', $5)
        """,
        uuid4(), user_id, now - timedelta(days=200), event_id, now,
    )
    await connection.execute(
        """
        INSERT INTO customer_activity_projection
            (channel, user_id, yclients_client_id, identity_status,
             identity_source, identity_verified_at, last_completed_visit_at,
             history_synced_at, recent_bookings_synced_at, source_version,
             sync_status, updated_at)
        VALUES ('telegram', $1, $2, 'verified', 'test', $3, $4,
                $3, $3, 'test-v1', 'current', $3)
        """,
        user_id, f"client-{user_id}", now,
        now - timedelta(days=200),
    )


async def test_tick_creates_one_main_step_and_outbox(planner):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "70001")

    assert await repository.run_planner_cycle(
        NOW, planner_limit=100, step_claim_limit=100
    ) == 1

    async with database.acquire() as connection:
        journey = await connection.fetchrow(
            "SELECT id, status FROM reactivation_journeys WHERE user_id = '70001'"
        )
        steps = await connection.fetch(
            "SELECT step_kind, status, outbound_id, idempotency_key "
            "FROM reactivation_journey_steps WHERE journey_id = $1", journey["id"]
        )
        keys = await connection.fetch(
            "SELECT idempotency_key FROM task_outbox ORDER BY created_at, id"
        )
    assert journey["status"] == "scheduled"
    assert [row["step_kind"] for row in steps] == ["main"]
    assert steps[0]["status"] == "reserved"
    assert steps[0]["outbound_id"] is not None
    assert steps[0]["idempotency_key"] == f"reactivation:{journey['id']}:main"
    assert [row["idempotency_key"] for row in keys] == [
        f"send_outbound:{steps[0]['outbound_id']}"
    ]


async def test_dry_run_and_stale_activity_fail_closed(planner):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "70002")
        await connection.execute(
            "UPDATE reactivation_settings SET mode = 'dry_run' WHERE id = 1"
        )
    assert await repository.run_planner_cycle(NOW) == 0
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE reactivation_settings SET mode = 'active' WHERE id = 1"
        )
        await connection.execute(
            "UPDATE customer_activity_projection "
            "SET recent_bookings_synced_at = $1 WHERE user_id = '70002'",
            NOW - timedelta(minutes=16),
        )
    assert await repository.run_planner_cycle(NOW) == 0
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM reactivation_journeys") == 0
        assert await connection.fetchval("SELECT count(*) FROM outbound_messages") == 0


async def test_replay_and_concurrent_ticks_create_one_journey(planner):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "70003")

    await asyncio.gather(
        repository.run_planner_cycle(NOW), repository.run_planner_cycle(NOW)
    )
    await repository.run_planner_cycle(NOW)

    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM reactivation_journeys") == 1
        assert await connection.fetchval("SELECT count(*) FROM reactivation_journey_steps") == 1
        assert await connection.fetchval("SELECT count(*) FROM outbound_messages") == 1


async def test_main_sent_schedules_one_reminder_from_actual_sent_time(planner):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "70004")
    await repository.run_planner_cycle(NOW)
    sent_at = NOW + timedelta(hours=2)
    async with database.acquire() as connection:
        outbound_id = await connection.fetchval(
            "SELECT outbound_id FROM reactivation_journey_steps WHERE step_kind = 'main'"
        )

    assert await repository.record_delivery_sent(outbound_id, sent_at)
    assert not await repository.record_delivery_sent(outbound_id, sent_at + timedelta(seconds=1))

    async with database.acquire() as connection:
        journey = await connection.fetchrow(
            "SELECT id, status, first_sent_at FROM reactivation_journeys"
        )
        reminder = await connection.fetchrow(
            "SELECT due_at, status FROM reactivation_journey_steps "
            "WHERE step_kind = 'reminder'"
        )
    assert tuple(journey.values())[1:] == ("active", sent_at)
    assert reminder["due_at"] == next_send_at(sent_at + timedelta(days=5))
    assert reminder["status"] == "scheduled"


async def test_main_sent_closes_exhausted_when_reminder_is_disabled(planner):
    database, repository, version_id = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "70005")
        await connection.execute(
            "UPDATE reactivation_program_versions "
            "SET reminder_enabled = false, reminder_after_days = NULL WHERE id = $1",
            version_id,
        )
    await repository.run_planner_cycle(NOW)
    async with database.acquire() as connection:
        outbound_id = await connection.fetchval(
            "SELECT outbound_id FROM reactivation_journey_steps WHERE step_kind = 'main'"
        )

    assert await repository.record_delivery_sent(outbound_id, NOW + timedelta(minutes=1))
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT close_reason FROM reactivation_journeys"
        ) == "exhausted"
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journey_steps WHERE step_kind = 'reminder'"
        ) == 0


@pytest.mark.parametrize("terminal", ["failed", "delivery_unknown"])
async def test_terminal_main_never_schedules_reminder(planner, terminal):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, f"7200{terminal}")
    await repository.run_planner_cycle(NOW)
    async with database.acquire() as connection:
        outbound_id = await connection.fetchval(
            "SELECT outbound_id FROM reactivation_journey_steps WHERE step_kind = 'main'"
        )
        await connection.execute(
            "UPDATE reactivation_journey_steps SET status = $1 WHERE outbound_id = $2",
            terminal, outbound_id,
        )

    assert not await repository.record_delivery_sent(outbound_id, NOW + timedelta(minutes=1))
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journey_steps WHERE step_kind = 'reminder'"
        ) == 0


async def test_missing_legal_gate_creates_nothing(planner):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "70006")
        await connection.execute(
            "UPDATE reactivation_settings SET legal_status = 'pending' WHERE id = 1"
        )

    assert await repository.run_planner_cycle(NOW) == 0
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM reactivation_journeys") == 0


async def test_planner_is_bounded_to_one_hundred_new_journeys(planner):
    database, repository, _ = planner
    async with database.acquire() as connection:
        for value in range(101):
            await _seed_eligible(connection, f"8{value:04d}")

    assert await repository.run_planner_cycle(NOW, step_claim_limit=0) == 0
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM reactivation_journeys") == 100


async def test_planner_limit_counts_eligible_not_earlier_excluded_rows(planner):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "80000")
        await _seed_eligible(connection, "80001")
        await connection.execute(
            "UPDATE marketing_consents SET active = false, updated_at = $2 "
            "WHERE user_id = $1",
            "80000", NOW - timedelta(minutes=1),
        )

    await repository.run_planner_cycle(NOW, planner_limit=1, step_claim_limit=0)

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT user_id FROM reactivation_journeys"
        ) == "80001"


@pytest.mark.parametrize("reason", ["reply", "booking", "stop"])
async def test_outcome_before_delivery_prevents_reminder(planner, reason):
    database, repository, _ = planner
    user_id = f"7100{reason}"
    async with database.acquire() as connection:
        await _seed_eligible(connection, user_id)
    await repository.run_planner_cycle(NOW)
    async with database.acquire() as connection:
        outbound_id = await connection.fetchval(
            "SELECT outbound_id FROM reactivation_journey_steps WHERE step_kind = 'main'"
        )
        if reason == "reply":
            await connection.execute(
                "UPDATE customer_activity_projection SET last_meaningful_inbound_at = $2 "
                "WHERE user_id = $1", user_id, NOW + timedelta(minutes=1),
            )
        elif reason == "booking":
            await connection.execute(
                "UPDATE customer_activity_projection SET next_active_booking_at = $2 "
                "WHERE user_id = $1", user_id, NOW + timedelta(days=1),
            )
        else:
            await connection.execute(
                "UPDATE marketing_consents SET active = false, revoked_at = $2, updated_at = $2 "
                "WHERE user_id = $1", user_id, NOW + timedelta(minutes=1),
            )
    await repository.run_planner_cycle(NOW + timedelta(minutes=2))
    await repository.record_delivery_sent(outbound_id, NOW + timedelta(minutes=3))
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journey_steps WHERE step_kind = 'reminder'"
        ) == 0


async def test_yclients_unavailable_forces_dry_run_idempotently(planner):
    database, repository, _ = planner
    job_id = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO scheduler_jobs
                (id, kind, run_at, payload, idempotency_key, status, attempts)
            VALUES ($1, 'reactivation_activity_sync', $2, '{}'::jsonb,
                    'reactivation_activity_sync:test', 'pending', 0)
            """,
            job_id, NOW,
        )

    assert await repository.fail_closed_yclients_unavailable(NOW)
    assert not await repository.fail_closed_yclients_unavailable(
        NOW + timedelta(seconds=1)
    )

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT mode FROM reactivation_settings WHERE id = 1"
        ) == "dry_run"
        assert await connection.fetchval(
            "SELECT status FROM scheduler_jobs WHERE id = $1", job_id
        ) == "skipped"
