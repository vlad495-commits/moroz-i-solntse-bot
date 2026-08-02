import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.booking.models import (
    BookingScenario,
    BookingTemporaryError,
    ExternalBooking,
)
from moroz.booking.reconciliation import BookingReconciler
from moroz.booking.repository import BookingRepository
from moroz.common.db import Database


pytestmark = pytest.mark.asyncio
pytest_plugins = ("tests.integration.conftest",)
NOW = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=4)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def repository(database):
    return BookingRepository(database, staff_chat_id="900001")


@pytest.fixture
def scenario():
    scenario_id = uuid4()
    return BookingScenario(
        id=scenario_id,
        kind="create",
        phase="executing",
        idempotency_key=f"booking:{scenario_id}",
        customer_id="700001",
        state={
            "selected_slot_id": "slot-1",
            "selected_service_ids": ["service-1", "service-2"],
            "actual_staff_id": "staff-1",
            "starts_at": (NOW + timedelta(days=2)).isoformat(),
            "duration_minutes": 90,
        },
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )


def exact_match(scenario):
    starts_at = datetime.fromisoformat(str(scenario.state["starts_at"]))
    booking_key = (
        scenario.id
        if scenario.kind == "create"
        else UUID(str(scenario.state["booking_key"]))
    )
    return ExternalBooking(
        external_id="provider-42",
        customer_id=scenario.customer_id,
        booking_key=booking_key,
        slot_id=str(scenario.state["selected_slot_id"]),
        service_ids=tuple(scenario.state["selected_service_ids"]),
        staff_id=str(scenario.state["actual_staff_id"]),
        starts_at=starts_at,
        scheduled_end_at=starts_at + timedelta(minutes=90),
        status="confirmed",
    )


def change_scenario_and_match(scenario, kind):
    booking_key = uuid4()
    starts_at = NOW + timedelta(days=2)
    new_start = NOW + timedelta(days=3)
    state = {
        "booking_key": str(booking_key),
        "external_id": "provider-42",
        "selected_service_ids": ["service-1", "service-2"],
        "original_slot_id": "slot-old",
        "old_staff_id": "staff-old",
        "starts_at": starts_at.isoformat(),
        "old_scheduled_end_at": (starts_at + timedelta(minutes=90)).isoformat(),
    }
    if kind == "reschedule":
        state.update(
            {
                "selected_slot_id": "slot-new",
                "actual_staff_id": "staff-new",
                "selected_new_starts_at": new_start.isoformat(),
                "duration_minutes": 60,
            }
        )
        found = ExternalBooking(
            "provider-42",
            scenario.customer_id,
            booking_key,
            "slot-new",
            ("service-2", "service-1"),
            "staff-new",
            new_start,
            "confirmed",
            new_start + timedelta(minutes=60),
        )
    else:
        found = ExternalBooking(
            "provider-42",
            scenario.customer_id,
            booking_key,
            "slot-old",
            ("service-2", "service-1"),
            "staff-old",
            starts_at,
            "cancelled",
            starts_at + timedelta(minutes=90),
        )
    return replace(scenario, kind=kind, state=state), found


class Lookup:
    def __init__(self, matches=()):
        self.matches = list(matches)
        self.calls = []
        self.error = None

    async def find_by_booking_key(self, booking_key):
        self.calls.append(booking_key)
        if self.error is not None:
            raise self.error
        return list(self.matches)


async def escalate(repository, scenario):
    await repository.create_scenario(scenario)
    await repository.escalate(scenario, "booking_outcome_unknown")


async def durable_state(database, scenario_id):
    async with database.acquire() as connection:
        return await connection.fetchrow(
            """
            SELECT s.phase, s.error_code, e.status escalation_status,
                   h.enabled, count(b.id) booking_count
            FROM booking_scenarios s
            JOIN escalations e ON e.payload->>'scenario_id'=s.id::text
            JOIN human_mode h ON h.escalation_id=e.id
            LEFT JOIN bookings b ON b.last_scenario_id=s.id
            WHERE s.id=$1
            GROUP BY s.phase, s.error_code, e.status, h.enabled
            """,
            scenario_id,
        )


async def test_one_exact_match_atomically_confirms_and_closes_linked_escalation(
    database, repository, scenario
):
    await escalate(repository, scenario)
    lookup = Lookup([exact_match(scenario)])

    result = await BookingReconciler(lookup, repository).reconcile(scenario.id)

    assert result.status == "confirmed"
    assert lookup.calls == [scenario.id]
    state = await durable_state(database, scenario.id)
    assert tuple(state.values()) == ("confirmed", None, "resolved", False, 1)
    assert await repository.get_local_booking(scenario.id) == exact_match(scenario)
    events = await repository.list_events(scenario.id)
    reconciled = [event for event in events if event.event_type == "booking_reconciled"]
    assert len(reconciled) == 1
    assert reconciled[0].payload == {
        "reason_code": "booking_reconciled_exact_match"
    }


@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
async def test_change_outcome_reconciles_against_kind_specific_exact_snapshot(
    database, repository, scenario, kind
):
    changed, found = change_scenario_and_match(scenario, kind)
    await escalate(repository, changed)
    lookup = Lookup([found])

    result = await BookingReconciler(lookup, repository).reconcile(changed.id)

    assert result.status == "confirmed"
    assert changed.id != found.booking_key
    assert lookup.calls == [found.booking_key]
    assert await repository.get_local_booking(changed.id) == found
    events = [event.event_type for event in await repository.list_events(changed.id)]
    assert ("booking_cancelled" if kind == "cancel" else "booking_confirmed") in events


@pytest.mark.parametrize(
    "raw_booking_key",
    [None, "", "not-a-uuid", "00000000-0000-0000-0000-000000000000"],
)
@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
async def test_change_invalid_booking_key_fails_before_lookup_or_local_write(
    database, repository, scenario, kind, raw_booking_key
):
    changed, found = change_scenario_and_match(scenario, kind)
    state = dict(changed.state)
    if raw_booking_key is None:
        state.pop("booking_key")
    else:
        state["booking_key"] = raw_booking_key
    changed = replace(changed, state=state)
    await escalate(repository, changed)
    before_events = await repository.list_events(changed.id)
    lookup = Lookup([found])

    result = await BookingReconciler(lookup, repository).reconcile(changed.id)

    assert result.reason_code == "booking_reconciliation_not_eligible"
    assert lookup.calls == []
    assert await repository.list_events(changed.id) == before_events
    assert (await durable_state(database, changed.id))["escalation_status"] == "open"


@pytest.mark.parametrize("kind", ["create", "cancel"])
async def test_ordinary_terminal_scenario_without_reconciliation_marker_is_not_replayed(
    database, repository, scenario, kind
):
    terminal, booking = (
        (scenario, exact_match(scenario))
        if kind == "create"
        else change_scenario_and_match(scenario, "cancel")
    )
    terminal = replace(terminal, phase="confirmed")
    await repository.create_scenario(terminal)
    if kind == "cancel":
        await repository.complete_cancellation(terminal, booking)
    else:
        await repository.confirm(terminal, booking)
    lookup = Lookup([booking])
    before_events = await repository.list_events(terminal.id)

    result = await BookingReconciler(lookup, repository).reconcile(terminal.id)

    assert result.reason_code == "booking_reconciliation_not_eligible"
    assert lookup.calls == []
    assert await repository.list_events(terminal.id) == before_events


@pytest.mark.parametrize(
    ("kind", "change"),
    [
        ("reschedule", {"booking_key": uuid4()}),
        ("reschedule", {"customer_id": "foreign"}),
        ("reschedule", {"slot_id": "other-new-slot"}),
        ("reschedule", {"service_ids": ("service-1",)}),
        ("reschedule", {"staff_id": "other-new-staff"}),
        ("reschedule", {"starts_at": NOW + timedelta(days=3, minutes=1)}),
        ("reschedule", {"scheduled_end_at": NOW + timedelta(days=3, minutes=59)}),
        ("reschedule", {"status": "cancelled"}),
        ("cancel", {"booking_key": uuid4()}),
        ("cancel", {"customer_id": "foreign"}),
        ("cancel", {"slot_id": "other-old-slot"}),
        ("cancel", {"service_ids": ("service-1",)}),
        ("cancel", {"staff_id": "other-old-staff"}),
        ("cancel", {"starts_at": NOW + timedelta(days=2, minutes=1)}),
        ("cancel", {"scheduled_end_at": NOW + timedelta(days=2, minutes=89)}),
        ("cancel", {"status": "confirmed"}),
    ],
)
async def test_change_full_negative_snapshot_matrix_keeps_escalation_open(
    database, repository, scenario, kind, change
):
    changed, found = change_scenario_and_match(scenario, kind)
    await escalate(repository, changed)

    result = await BookingReconciler(
        Lookup([replace(found, **change)]), repository
    ).reconcile(changed.id)

    assert result.status == "escalated"
    assert tuple((await durable_state(database, changed.id)).values()) == (
        "escalated", "booking_outcome_unknown", "open", True, 0
    )


@pytest.mark.parametrize("kind", ["reschedule", "cancel"])
@pytest.mark.parametrize("outcome", ["zero", "multiple", "malformed", "temporary"])
async def test_change_non_exact_lookup_outcomes_keep_escalation_open(
    database, repository, scenario, kind, outcome
):
    changed, found = change_scenario_and_match(scenario, kind)
    await escalate(repository, changed)
    matches = {
        "zero": [],
        "multiple": [found, found],
        "malformed": [object()],
        "temporary": [],
    }[outcome]
    lookup = Lookup(matches)
    if outcome == "temporary":
        lookup.error = BookingTemporaryError()

    result = await BookingReconciler(lookup, repository).reconcile(changed.id)

    assert result.status == "escalated"
    assert tuple((await durable_state(database, changed.id)).values()) == (
        "escalated", "booking_outcome_unknown", "open", True, 0
    )


@pytest.mark.parametrize(
    "change",
    [
        {"customer_id": "foreign"},
        {"booking_key": uuid4()},
        {"slot_id": "other-slot"},
        {"service_ids": ("service-1",)},
        {"service_ids": ("service-1", "service-1")},
        {"staff_id": "other-staff"},
        {"starts_at": NOW + timedelta(days=2, minutes=1)},
        {"scheduled_end_at": NOW + timedelta(days=2, minutes=89)},
        {"status": "cancelled"},
    ],
)
async def test_mismatch_keeps_escalation_open(database, repository, scenario, change):
    await escalate(repository, scenario)
    lookup = Lookup([replace(exact_match(scenario), **change)])

    result = await BookingReconciler(lookup, repository).reconcile(scenario.id)

    assert result.status == "escalated"
    assert tuple((await durable_state(database, scenario.id)).values()) == (
        "escalated",
        "booking_outcome_unknown",
        "open",
        True,
        0,
    )


@pytest.mark.parametrize("matches", [[], "multiple"])
async def test_zero_or_multiple_matches_keep_escalation_open(
    database, repository, scenario, matches
):
    await escalate(repository, scenario)
    found = [] if matches == [] else [exact_match(scenario), exact_match(scenario)]

    result = await BookingReconciler(Lookup(found), repository).reconcile(scenario.id)

    assert result.status == "escalated"
    assert (await durable_state(database, scenario.id))["escalation_status"] == "open"


async def test_temporary_lookup_error_keeps_escalation_open(
    database, repository, scenario
):
    await escalate(repository, scenario)
    lookup = Lookup()
    lookup.error = BookingTemporaryError()

    result = await BookingReconciler(lookup, repository).reconcile(scenario.id)

    assert result.status == "escalated"
    assert (await durable_state(database, scenario.id))["enabled"] is True


async def test_replay_and_concurrency_resolve_once(database, repository, scenario):
    await escalate(repository, scenario)
    first = BookingRepository(database, staff_chat_id="900001")
    second = BookingRepository(database, staff_chat_id="900001")
    lookup = Lookup([exact_match(scenario)])

    results = await asyncio.gather(
        BookingReconciler(lookup, first).reconcile(scenario.id),
        BookingReconciler(lookup, second).reconcile(scenario.id),
    )
    replay = await BookingReconciler(lookup, first).reconcile(scenario.id)

    assert {item.status for item in [*results, replay]} == {"confirmed"}
    async with database.acquire() as connection:
        counts = await connection.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM bookings WHERE last_scenario_id=$1) bookings,
              (SELECT count(*) FROM booking_events
               WHERE scenario_id=$1 AND event_type='booking_reconciled') events
            """,
            scenario.id,
        )
    assert tuple(counts.values()) == (1, 1)


async def test_resolution_rolls_back_all_local_changes_on_late_failure(
    database, repository, scenario
):
    await escalate(repository, scenario)
    async with database.acquire() as connection:
        await connection.execute(
            """
            CREATE FUNCTION reject_reconciliation() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.event_type = 'booking_reconciled' THEN
                    RAISE EXCEPTION 'forced reconciliation failure';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER reject_reconciliation_insert
            BEFORE INSERT ON booking_events
            FOR EACH ROW EXECUTE FUNCTION reject_reconciliation();
            """
        )

    with pytest.raises(asyncpg.PostgresError, match="forced reconciliation failure"):
        await BookingReconciler(
            Lookup([exact_match(scenario)]), repository
        ).reconcile(scenario.id)

    assert tuple((await durable_state(database, scenario.id)).values()) == (
        "escalated",
        "booking_outcome_unknown",
        "open",
        True,
        0,
    )
