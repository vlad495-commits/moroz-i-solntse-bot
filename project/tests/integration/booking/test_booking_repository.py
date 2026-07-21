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
    return BookingRepository(database)


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
        slot_id="slot-9",
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
            SELECT last_scenario_id, snapshot
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
    snapshot = json.loads(row["snapshot"])
    assert snapshot["external_id"] == booking.external_id
    assert snapshot["starts_at"] == booking.starts_at.isoformat()


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
        "provider_outcome_unknown",
        {"operation": "create", "attempts": [1, 2]},
    )

    stored = await repo.get_scenario(scenario.id)
    events = await repo.list_events(scenario.id)
    assert stored.phase == "escalated"
    assert stored.error_code == "provider_outcome_unknown"
    assert events[-1].event_type == "admin_attention_required"
    assert events[-1].payload == {
        "operation": "create",
        "attempts": (1, 2),
        "error_code": "provider_outcome_unknown",
    }


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
