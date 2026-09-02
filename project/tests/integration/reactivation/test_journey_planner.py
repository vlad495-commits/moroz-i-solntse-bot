import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.notifications.repository import SchedulerJobRepository
from moroz.privacy import customer_lock_subject
from moroz.reactivation.activity import ActivityCandidate, ActivityRepository
from moroz.reactivation.policy import ProgramPolicy, next_send_at, template_checksum
from moroz.reactivation.repository import ActivationBlocked, ReactivationRepository
from moroz.reactivation.service import (
    ReactivationCoordinator,
    reactivation_activity_sync_job,
    reactivation_tick_job,
)


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
        delivery_options = await connection.fetchval(
            "SELECT delivery_options FROM outbound_messages WHERE id = $1",
            steps[0]["outbound_id"],
        )
        if isinstance(delivery_options, str):
            delivery_options = json.loads(delivery_options)
    assert journey["status"] == "scheduled"
    assert [row["step_kind"] for row in steps] == ["main"]
    assert steps[0]["status"] == "reserved"
    assert steps[0]["outbound_id"] is not None
    assert steps[0]["idempotency_key"] == f"reactivation:{journey['id']}:main"
    assert [row["idempotency_key"] for row in keys] == [
        f"send_outbound:{steps[0]['outbound_id']}"
    ]
    assert delivery_options["reply_markup"]["inline_keyboard"] == [
        [{"text": "Записаться", "callback_data": "reactivation:book"}],
        [{"text": "Задать вопрос", "callback_data": "reactivation:ask"}],
        [{"text": "Не писать", "callback_data": "marketing:disable"}],
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


async def test_planner_does_not_depend_on_deprecated_legal_fields(planner):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "70006")
        await connection.execute(
            "UPDATE reactivation_settings SET legal_status = 'pending', "
            "legal_reference = NULL, legal_approved_at = NULL, "
            "legal_approved_by = NULL WHERE id = 1"
        )

    assert await repository.run_planner_cycle(NOW) == 1
    async with database.acquire() as connection:
        assert await connection.fetchval("SELECT count(*) FROM reactivation_journeys") == 1


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


async def test_booking_writer_commit_before_fenced_recheck_prevents_enqueue(planner):
    database, repository, _ = planner
    user_id = "race-booking"
    async with database.acquire() as connection:
        await _seed_eligible(connection, user_id)
    writer = await database._pool.acquire()
    transaction = writer.transaction()
    await transaction.start()
    await writer.execute(
        "UPDATE customer_activity_projection SET next_active_booking_at = $2 "
        "WHERE user_id = $1",
        user_id, NOW + timedelta(days=1),
    )
    task = asyncio.create_task(repository.run_planner_cycle(NOW))
    await asyncio.sleep(0.1)
    assert not task.done()
    await transaction.commit()
    await database._pool.release(writer)

    assert await asyncio.wait_for(task, 3) == 0
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journeys WHERE user_id = $1", user_id
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM outbound_messages WHERE chat_id = $1", user_id
        ) == 0


@pytest.mark.parametrize("writer_kind", ["stop", "escalation", "deletion"])
async def test_customer_writer_commit_before_fenced_recheck_never_resurrects(
    planner, writer_kind
):
    database, repository, _ = planner
    user_id = f"race-{writer_kind}"
    async with database.acquire() as connection:
        await _seed_eligible(connection, user_id)
    writer = await database._pool.acquire()
    transaction = writer.transaction()
    await transaction.start()
    await writer.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        customer_lock_subject(user_id),
    )
    if writer_kind == "stop":
        await writer.execute(
            "UPDATE marketing_consents SET active = false, revoked_at = $2, "
            "updated_at = $2 WHERE user_id = $1",
            user_id, NOW + timedelta(seconds=1),
        )
    elif writer_kind == "escalation":
        await writer.execute(
            "INSERT INTO escalations "
            "(id, source, customer_id, status, reason_code, payload) "
            "VALUES ($1, 'test', $2, 'open', 'test', '{}'::jsonb)",
            uuid4(), user_id,
        )
    else:
        await writer.execute(
            "DELETE FROM marketing_consents WHERE user_id = $1", user_id
        )
        await writer.execute(
            "DELETE FROM customer_activity_projection WHERE user_id = $1", user_id
        )
    task = asyncio.create_task(repository.run_planner_cycle(NOW))
    await asyncio.sleep(0.1)
    assert not task.done()
    await transaction.commit()
    await database._pool.release(writer)

    assert await asyncio.wait_for(task, 3) == 0
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journeys WHERE user_id = $1", user_id
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM outbound_messages WHERE chat_id = $1", user_id
        ) == 0


async def test_due_step_claim_skips_row_locked_by_another_transaction(planner):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "step-lock-1")
        await _seed_eligible(connection, "step-lock-2")
    await repository.run_planner_cycle(NOW, step_claim_limit=0)
    locker = await database._pool.acquire()
    transaction = locker.transaction()
    await transaction.start()
    locked = await locker.fetchrow(
        "SELECT step.id, journey.user_id FROM reactivation_journey_steps AS step "
        "JOIN reactivation_journeys AS journey ON journey.id = step.journey_id "
        "WHERE step.status = 'scheduled' ORDER BY step.id LIMIT 1"
    )
    await locker.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        customer_lock_subject(locked["user_id"]),
    )
    await locker.execute(
        "SELECT 1 FROM reactivation_journey_steps WHERE id = $1 FOR UPDATE",
        locked["id"],
    )

    assert await asyncio.wait_for(
        repository.run_planner_cycle(NOW, planner_limit=0, step_claim_limit=1),
        3,
    ) == 1
    async with database.acquire() as connection:
        reserved = await connection.fetchval(
            "SELECT id FROM reactivation_journey_steps WHERE status = 'reserved'"
        )
    assert reserved != locked["id"]
    await transaction.rollback()
    await database._pool.release(locker)


async def test_outcome_refresh_is_deterministically_bounded_to_one_hundred(planner):
    database, repository, version_id = planner
    async with database.acquire() as connection:
        await connection.executemany(
            """
            INSERT INTO reactivation_journeys
                (id, channel, user_id, program_version_id, status,
                 activity_anchor_at, created_at, updated_at)
            VALUES ($1, 'telegram', $2, $3, 'scheduled', $4, $5, $5)
            """,
            [
                (
                    uuid4(), f"bounded-{index:03d}", version_id,
                    NOW - timedelta(days=200), NOW + timedelta(seconds=index),
                )
                for index in range(101)
            ],
        )

    await repository.run_planner_cycle(NOW + timedelta(minutes=5), planner_limit=0)

    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journeys WHERE status = 'closed'"
        ) == 100
        assert await connection.fetchval(
            "SELECT count(*) FROM reactivation_journeys WHERE status != 'closed'"
        ) == 1


@pytest.mark.parametrize("transition", ["activate", "mode"])
async def test_planner_first_serializes_with_owner_transition_without_deadlock(
    planner, transition
):
    database, repository, version_id = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, f"lock-{transition}")
        owner_id = await connection.fetchval(
            "SELECT id FROM admin_users WHERE username = 'planner-owner'"
        )
    owner = ReactivationRepository(database, session_secret="lock-test")
    await owner.preview_version(version_id, owner_id, NOW)
    if transition == "mode":
        await owner.set_mode("paused", owner_id, NOW)
    planner_has_fence = asyncio.Event()
    release_planner = asyncio.Event()
    original = repository._locked_runtime

    async def paused_runtime(connection):
        result = await original(connection)
        planner_has_fence.set()
        await release_planner.wait()
        return result

    repository._locked_runtime = paused_runtime
    planner_task = asyncio.create_task(repository.run_planner_cycle(NOW))
    await planner_has_fence.wait()
    transition_task = asyncio.create_task(
        owner.activate_version(version_id, owner_id, NOW)
        if transition == "activate"
        else owner.set_mode("active", owner_id, NOW)
    )
    await asyncio.sleep(0.1)
    assert not transition_task.done()
    release_planner.set()
    results = await asyncio.wait_for(
        asyncio.gather(planner_task, transition_task, return_exceptions=True), 5
    )

    assert not any(isinstance(item, asyncpg.DeadlockDetectedError) for item in results)
    assert not isinstance(results[0], Exception)
    assert not isinstance(results[1], Exception) or isinstance(
        results[1], ActivationBlocked
    )


@pytest.mark.parametrize("transition", ["activate", "mode"])
async def test_owner_transition_first_serializes_planner_without_deadlock(
    planner, transition
):
    database, repository, version_id = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, f"reverse-{transition}")
        owner_id = await connection.fetchval(
            "SELECT id FROM admin_users WHERE username = 'planner-owner'"
        )
    owner = ReactivationRepository(database, session_secret="lock-test")
    await owner.preview_version(version_id, owner_id, NOW)
    if transition == "mode":
        await owner.set_mode("paused", owner_id, NOW)
    transition_has_fence = asyncio.Event()
    release_transition = asyncio.Event()
    original = owner._population

    async def paused_population(connection, version, now):
        result = await original(connection, version, now)
        transition_has_fence.set()
        await release_transition.wait()
        return result

    owner._population = paused_population
    transition_task = asyncio.create_task(
        owner.activate_version(version_id, owner_id, NOW)
        if transition == "activate"
        else owner.set_mode("active", owner_id, NOW)
    )
    await transition_has_fence.wait()
    planner_task = asyncio.create_task(repository.run_planner_cycle(NOW))
    await asyncio.sleep(0.1)
    assert not planner_task.done()
    release_transition.set()
    results = await asyncio.wait_for(
        asyncio.gather(transition_task, planner_task, return_exceptions=True), 5
    )

    assert not any(isinstance(item, asyncpg.DeadlockDetectedError) for item in results)
    assert not any(isinstance(item, Exception) for item in results)


@pytest.mark.parametrize("start_order", ["planner", "identity"])
async def test_planner_and_identity_collision_never_deadlock(planner, start_order):
    database, repository, _ = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "identity-b", now=NOW - timedelta(seconds=1))
        await _seed_eligible(connection, "identity-a")
        await connection.execute(
            "UPDATE customer_activity_projection "
            "SET yclients_client_id = CASE user_id "
            "WHEN 'identity-a' THEN '1' ELSE '2' END "
            "WHERE user_id IN ('identity-a', 'identity-b')"
        )
    first_locked = asyncio.Event()
    release_planner = asyncio.Event()
    original = repository._locked_recipient_state
    calls = 0

    async def pause_after_first(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 1:
            first_locked.set()
            await release_planner.wait()
        return result

    repository._locked_recipient_state = pause_after_first
    activity = ActivityRepository(database)

    async def collide(connection):
        return await activity.resolve_identity(
            connection,
            ActivityCandidate(
                "telegram", "identity-a", "verified", "1"
            ),
            ("2",),
            now=NOW + timedelta(seconds=1),
        )

    if start_order == "identity":
        identity_has_a = asyncio.Event()

        async def identity_first():
            async with database.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "SET LOCAL deadlock_timeout = '100ms'"
                    )
                    await connection.fetch(
                        "SELECT 1 FROM customer_activity_projection "
                        "WHERE user_id = 'identity-a' FOR UPDATE"
                    )
                    identity_has_a.set()
                    await first_locked.wait()
                    return await collide(connection)

        identity_task = asyncio.create_task(identity_first())
        await identity_has_a.wait()
        planner_task = asyncio.create_task(
            repository.run_planner_cycle(NOW, step_claim_limit=0)
        )
    else:
        planner_task = asyncio.create_task(
            repository.run_planner_cycle(NOW, step_claim_limit=0)
        )
        await first_locked.wait()

        async def planner_first_identity():
            async with database.acquire() as connection:
                await connection.execute("SET deadlock_timeout = '100ms'")
                return await collide(connection)

        identity_task = asyncio.create_task(planner_first_identity())

    await first_locked.wait()
    await asyncio.sleep(0.2)
    identity_was_waiting = not identity_task.done()
    release_planner.set()
    results = await asyncio.wait_for(
        asyncio.gather(planner_task, identity_task, return_exceptions=True), 5
    )
    assert identity_was_waiting, results
    assert not any(isinstance(item, asyncpg.DeadlockDetectedError) for item in results)
    assert not any(isinstance(item, Exception) for item in results)


@pytest.mark.parametrize("start_order", ["outcome", "delivery"])
async def test_outcome_close_and_delivery_acceptance_never_deadlock(
    planner, start_order
):
    database, repository, version_id = planner
    async with database.acquire() as connection:
        await _seed_eligible(connection, "outcome-delivery")
        if start_order == "delivery":
            await connection.execute(
                "UPDATE reactivation_program_versions "
                "SET reminder_enabled = false, reminder_after_days = NULL WHERE id = $1",
                version_id,
            )
    assert await repository.run_planner_cycle(NOW) == 1
    async with database.acquire() as connection:
        outbound_id = await connection.fetchval(
            "SELECT outbound_id FROM reactivation_journey_steps "
            "WHERE status = 'reserved'"
        )
        await connection.execute(
            "UPDATE marketing_consents SET active = false, revoked_at = $1 "
            "WHERE user_id = 'outcome-delivery'",
            NOW + timedelta(seconds=1),
        )
    first_at_close = asyncio.Event()
    release_first = asyncio.Event()
    original = repository._close_journey
    calls = 0

    async def pause_first_close(connection, journey_id, reason, now):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_at_close.set()
            await release_first.wait()
        return await original(connection, journey_id, reason, now)

    repository._close_journey = pause_first_close
    delivery_repository = (
        repository
        if start_order == "delivery"
        else ReactivationRepository(database)
    )
    delivery_repository._close_journey = pause_first_close

    outcome = lambda: repository.run_planner_cycle(
        NOW + timedelta(minutes=1), planner_limit=0, step_claim_limit=0
    )
    delivery = lambda: delivery_repository.record_delivery_sent(
        outbound_id, NOW + timedelta(minutes=1)
    )
    first = asyncio.create_task(delivery() if start_order == "delivery" else outcome())
    await asyncio.wait_for(first_at_close.wait(), 3)
    second = asyncio.create_task(outcome() if start_order == "delivery" else delivery())
    await asyncio.sleep(0.2)
    second_was_waiting = not second.done()
    release_first.set()
    results = await asyncio.wait_for(
        asyncio.gather(first, second, return_exceptions=True), 5
    )
    if start_order == "outcome":
        assert second_was_waiting, results
    assert not any(isinstance(item, asyncpg.DeadlockDetectedError) for item in results)
    assert not any(isinstance(item, Exception) for item in results)


@pytest.mark.parametrize("recovery_minutes", [2, 11])
async def test_yclients_recovery_reopens_only_current_bucket_jobs(
    planner, recovery_minutes
):
    database, repository, _ = planner
    scheduler = SchedulerJobRepository(database)
    await scheduler.schedule(reactivation_activity_sync_job(NOW))
    await scheduler.schedule(reactivation_tick_job(NOW))
    await repository.fail_closed_yclients_unavailable(NOW + timedelta(minutes=1))
    recovery_at = NOW + timedelta(minutes=recovery_minutes)
    coordinator = ReactivationCoordinator(
        repository, scheduler, object(), clock=lambda: recovery_at
    )

    await coordinator.ensure_current(recovery_at)
    await coordinator.ensure_current(recovery_at)

    expected = (
        reactivation_activity_sync_job(recovery_at).idempotency_key,
        reactivation_tick_job(recovery_at).idempotency_key,
    )
    async with database.acquire() as connection:
        rows = await connection.fetch(
            "SELECT idempotency_key, status, last_error_code FROM scheduler_jobs "
            "WHERE idempotency_key = ANY($1::text[]) ORDER BY idempotency_key",
            list(expected),
        )
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"pending"}
    assert {row["last_error_code"] for row in rows} == {None}


async def test_yclients_recovery_never_reopens_unrelated_terminal_job(planner):
    database, repository, _ = planner
    scheduler = SchedulerJobRepository(database)
    job = reactivation_tick_job(NOW)
    await scheduler.schedule(job)
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE scheduler_jobs SET status = 'failed', "
            "last_error_code = 'database_unavailable', finished_at = $2 "
            "WHERE idempotency_key = $1",
            job.idempotency_key,
            NOW,
        )
    coordinator = ReactivationCoordinator(
        repository, scheduler, object(), clock=lambda: NOW
    )

    await coordinator.ensure_current(NOW)

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT status, last_error_code FROM scheduler_jobs "
            "WHERE idempotency_key = $1",
            job.idempotency_key,
        )
    assert tuple(row) == ("failed", "database_unavailable")
