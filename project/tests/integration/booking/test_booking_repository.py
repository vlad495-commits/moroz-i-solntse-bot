import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.booking.models import BookingScenario, ExternalBooking
from moroz.booking.repository import BookingRepository
from moroz.common.db import Database


pytestmark = pytest.mark.asyncio
BOOKING_KEY = uuid4()


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def repo(database):
    return BookingRepository(database, staff_chat_id="900001")


@pytest.fixture
def scenario():
    now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    return BookingScenario(
        id=uuid4(),
        kind="create",
        phase="awaiting_confirmation",
        idempotency_key=f"booking:{uuid4()}",
        customer_id="customer-7",
        state={"answers": ["first", {"service_ids": ["service-1"]}]},
        error_code=None,
        created_at=now,
        updated_at=now,
    )


def confirmed_booking(*, status="confirmed"):
    return ExternalBooking(
        external_id="yclients-42",
        customer_id="customer-7",
        booking_key=BOOKING_KEY,
        slot_id="slot-9",
        service_ids=("service-1", "service-2"),
        staff_id="staff-7",
        starts_at=datetime(2026, 7, 25, 14, 0, tzinfo=UTC),
        status=status,
    )


async def test_create_scenario_is_idempotent_without_overwriting_state(
    database, repo, scenario
):
    scenario_id = await repo.create_scenario(scenario)
    duplicate = replace(
        scenario,
        id=uuid4(),
        customer_id="other-customer",
        state={"changed": True},
    )

    assert await repo.create_scenario(duplicate) == scenario_id

    stored = await repo.get_scenario(scenario_id)
    async with database.acquire() as connection:
        counts = await connection.fetchrow(
            """
            SELECT
                (SELECT count(*) FROM booking_scenarios) AS scenarios,
                (SELECT count(*) FROM booking_events) AS events
            """
        )

    assert stored == scenario
    assert isinstance(stored.state, MappingProxyType)
    assert stored.state["answers"][1]["service_ids"] == ("service-1",)
    assert tuple(counts.values()) == (1, 1)


async def test_checkpoint_updates_state_and_appends_event(repo, scenario):
    scenario_id = await repo.create_scenario(scenario)
    executing = replace(
        scenario,
        phase="executing",
        updated_at=scenario.updated_at + timedelta(minutes=1),
    )

    await repo.checkpoint(
        executing,
        "booking_execution_started",
        {"attempts": [{"slot_ids": ["slot-9"]}]},
    )

    assert (await repo.get_scenario(scenario_id)).phase == "executing"
    events = await repo.list_events(scenario_id)
    assert [event.event_type for event in events] == [
        "booking_scenario_created",
        "booking_execution_started",
    ]
    assert events[1].payload["attempts"][0]["slot_ids"] == ("slot-9",)


async def test_confirm_atomically_persists_terminal_scenario_and_booking(
    database, repo, scenario
):
    executing = replace(scenario, phase="executing")
    await repo.create_scenario(executing)
    terminal = replace(
        executing,
        phase="confirmed",
        updated_at=executing.updated_at + timedelta(minutes=1),
    )
    booking = confirmed_booking()

    await repo.confirm(terminal, booking)

    stored_scenario = await repo.get_scenario(scenario.id)
    stored_booking = await repo.get_local_booking(scenario.id)
    events = await repo.list_events(scenario.id)
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT last_scenario_id, booking_key, snapshot
            FROM bookings
            WHERE external_id = $1
            """,
            booking.external_id,
        )

    assert stored_scenario.phase == "confirmed"
    assert stored_scenario.state["external_id"] == booking.external_id
    assert stored_booking == booking
    assert [event.event_type for event in events] == [
        "booking_scenario_created",
        "booking_confirmed",
    ]
    assert row["last_scenario_id"] == scenario.id
    assert row["booking_key"] == booking.booking_key
    snapshot = json.loads(row["snapshot"])
    assert snapshot["external_id"] == booking.external_id
    assert snapshot["booking_key"] == str(booking.booking_key)
    assert snapshot["service_ids"] == list(booking.service_ids)
    assert snapshot["staff_id"] == booking.staff_id
    assert snapshot["starts_at"] == booking.starts_at.isoformat()


async def test_confirm_round_trips_scheduled_end_at(repo, scenario):
    executing = replace(scenario, phase="executing")
    await repo.create_scenario(executing)
    original = confirmed_booking()
    booking = replace(
        original,
        scheduled_end_at=original.starts_at + timedelta(hours=1),
    )

    await repo.confirm(replace(executing, phase="confirmed"), booking)

    stored = await repo.get_local_booking(scenario.id)
    assert stored is not None
    assert stored.scheduled_end_at == booking.scheduled_end_at


async def test_confirm_schedules_notifications_in_same_transaction(
    database, repo, scenario
):
    executing = replace(scenario, phase="executing")
    await repo.create_scenario(executing)
    terminal = replace(executing, phase="confirmed")
    booking = confirmed_booking()

    await repo.confirm(terminal, booking)

    async with database.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT kind, booking_starts_at, status
            FROM scheduler_jobs
            WHERE booking_key = $1
            ORDER BY run_at, kind
            """,
            booking.booking_key,
        )

    assert {row["kind"] for row in rows} == {
        "booking_created",
        "day_before",
        "morning",
        "hour_before",
        "no_show_check",
    }
    assert {row["booking_starts_at"] for row in rows} == {booking.starts_at}
    assert {row["status"] for row in rows} == {"pending"}


async def test_reschedule_replaces_old_notification_schedule(
    database, repo, scenario
):
    create_scenario = replace(scenario, phase="executing")
    await repo.create_scenario(create_scenario)
    original = confirmed_booking()
    await repo.confirm(replace(create_scenario, phase="confirmed"), original)

    reschedule_scenario = replace(
        scenario,
        id=uuid4(),
        kind="reschedule",
        phase="executing",
        idempotency_key=f"reschedule:{uuid4()}",
        updated_at=scenario.updated_at + timedelta(hours=1),
    )
    await repo.create_scenario(reschedule_scenario)
    moved = replace(original, starts_at=original.starts_at + timedelta(days=1))

    await repo.confirm(
        replace(reschedule_scenario, phase="confirmed"),
        moved,
    )

    async with database.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT booking_starts_at, status, count(*) AS jobs
            FROM scheduler_jobs
            WHERE booking_key = $1
            GROUP BY booking_starts_at, status
            ORDER BY booking_starts_at, status
            """,
            original.booking_key,
        )

    assert [
        (row["booking_starts_at"], row["status"], row["jobs"])
        for row in rows
    ] == [
        (original.starts_at, "skipped", 5),
        (moved.starts_at, "pending", 5),
    ]


async def test_cancellation_invalidates_pending_notifications(
    database, repo, scenario
):
    create_scenario = replace(scenario, phase="executing")
    await repo.create_scenario(create_scenario)
    booking = confirmed_booking()
    await repo.confirm(replace(create_scenario, phase="confirmed"), booking)

    cancel_scenario = replace(
        scenario,
        id=uuid4(),
        kind="cancel",
        phase="executing",
        idempotency_key=f"cancel:{uuid4()}",
    )
    await repo.create_scenario(cancel_scenario)

    await repo.complete_cancellation(
        replace(cancel_scenario, phase="confirmed"),
        replace(booking, status="cancelled"),
    )

    async with database.acquire() as connection:
        statuses = await connection.fetch(
            """
            SELECT DISTINCT status
            FROM scheduler_jobs
            WHERE booking_key = $1
            """,
            booking.booking_key,
        )

    assert [row["status"] for row in statuses] == ["skipped"]


async def test_confirm_rolls_back_booking_and_scenario_when_event_fails(
    database, repo, scenario
):
    executing = replace(scenario, phase="executing")
    await repo.create_scenario(executing)
    terminal = replace(executing, phase="confirmed")
    async with database.acquire() as connection:
        await connection.execute(
            """
            CREATE FUNCTION reject_booking_confirmed() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.event_type = 'booking_confirmed' THEN
                    RAISE EXCEPTION 'forced booking event failure';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER reject_booking_confirmed_insert
            BEFORE INSERT ON booking_events
            FOR EACH ROW EXECUTE FUNCTION reject_booking_confirmed();
            """
        )

    with pytest.raises(asyncpg.PostgresError, match="forced booking event failure"):
        await repo.confirm(terminal, confirmed_booking())

    assert (await repo.get_scenario(scenario.id)).phase == "executing"
    assert await repo.get_local_booking(scenario.id) is None
    assert [event.event_type for event in await repo.list_events(scenario.id)] == [
        "booking_scenario_created"
    ]


async def test_escalate_stores_error_and_admin_attention_event(repo, scenario):
    executing = replace(scenario, phase="executing")
    await repo.create_scenario(executing)
    escalated = replace(executing, phase="escalated")

    await repo.escalate(
        escalated,
        "booking_outcome_unknown",
        {"operation": "create", "attempts": [1, 2]},
    )

    stored = await repo.get_scenario(scenario.id)
    events = await repo.list_events(scenario.id)
    assert stored.phase == "escalated"
    assert stored.error_code == "booking_outcome_unknown"
    assert events[-1].event_type == "admin_attention_required"
    assert events[-1].payload == {"error_code": "booking_outcome_unknown"}


async def test_cancellation_upserts_snapshot_resolvable_by_earlier_scenario(
    database, repo, scenario
):
    create_terminal = replace(scenario, phase="confirmed")
    await repo.create_scenario(scenario)
    await repo.confirm(create_terminal, confirmed_booking())
    cancel_scenario = replace(
        scenario,
        id=uuid4(),
        kind="cancel",
        phase="executing",
        idempotency_key=f"cancel:{uuid4()}",
    )
    await repo.create_scenario(cancel_scenario)
    cancel_terminal = replace(cancel_scenario, phase="confirmed")
    cancelled = confirmed_booking(status="cancelled")

    await repo.complete_cancellation(cancel_terminal, cancelled)

    assert await repo.get_local_booking(scenario.id) == cancelled
    assert await repo.get_local_booking(cancel_scenario.id) == cancelled
    assert [
        event.event_type for event in await repo.list_events(cancel_scenario.id)
    ] == ["booking_scenario_created", "booking_cancelled"]
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT last_scenario_id FROM bookings"
        )
        count = await connection.fetchval("SELECT count(*) FROM bookings")
    assert count == 1
    assert row["last_scenario_id"] == cancel_scenario.id


async def _assert_ownership_conflict_rolls_back(
    database,
    repo,
    scenario: BookingScenario,
    conflicting_booking: ExternalBooking,
) -> None:
    original = confirmed_booking()
    origin = replace(scenario, phase="executing")
    await repo.create_scenario(origin)
    await repo.confirm(replace(origin, phase="confirmed"), original)

    contender = replace(
        scenario,
        id=uuid4(),
        phase="executing",
        idempotency_key=f"booking:{uuid4()}",
        customer_id=conflicting_booking.customer_id,
    )
    await repo.create_scenario(contender)
    before_scenario = await repo.get_scenario(contender.id)
    before_events = await repo.list_events(contender.id)

    with pytest.raises(RuntimeError, match="^booking ownership conflict$"):
        await repo.confirm(replace(contender, phase="confirmed"), conflicting_booking)

    assert await repo.get_scenario(contender.id) == before_scenario
    assert await repo.list_events(contender.id) == before_events
    assert await repo.get_local_booking(origin.id) == original
    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT customer_id, booking_key, snapshot FROM bookings WHERE external_id = $1",
            original.external_id,
        )
    assert row["customer_id"] == original.customer_id
    assert row["booking_key"] == original.booking_key
    assert json.loads(row["snapshot"])["booking_key"] == str(original.booking_key)


async def test_confirm_rejects_external_id_with_different_booking_key_and_rolls_back(
    database, repo, scenario
):
    await _assert_ownership_conflict_rolls_back(
        database,
        repo,
        scenario,
        replace(confirmed_booking(), booking_key=uuid4()),
    )


async def test_confirm_rejects_external_id_with_different_customer_and_rolls_back(
    database, repo, scenario
):
    await _assert_ownership_conflict_rolls_back(
        database,
        repo,
        scenario,
        replace(confirmed_booking(), customer_id="customer-other"),
    )
