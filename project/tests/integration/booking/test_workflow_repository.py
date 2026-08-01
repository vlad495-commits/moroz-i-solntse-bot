import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.booking.models import ExternalBooking, Slot
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.booking.workflow_repository import BookingWorkflowRepository
from moroz.common.db import Database


pytestmark = pytest.mark.asyncio


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=5)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repository(database, clock):
    return BookingWorkflowRepository(database, now=clock.now)


async def test_action_is_owner_revision_and_expiry_bound(repository, clock):
    scenario = await repository.start(
        "create", "telegram", "10", "10", "start:1"
    )
    action = await repository.issue_action(
        scenario.id,
        scenario.revision,
        "choose_service",
        {"service_id": "1"},
        clock.now() + timedelta(minutes=30),
    )

    assert await repository.consume_action(
        action.id, "telegram", "10", "10"
    ) == action
    assert await repository.consume_action(
        action.id, "telegram", "11", "11"
    ) is None

    advanced = await repository.checkpoint(
        replace(scenario, state={"service_ids": ["1"]}),
        "booking_service_selected",
    )
    assert advanced.revision == scenario.revision + 1
    assert await repository.consume_action(
        action.id, "telegram", "10", "10"
    ) is None


async def test_consumed_action_replays_saved_result_after_revision_and_expiry(
    repository, clock
):
    scenario = await repository.start(
        "create", "telegram", "10", "10", "start:replay"
    )
    action = await repository.issue_action(
        scenario.id,
        scenario.revision,
        "confirm",
        {},
        clock.now() + timedelta(minutes=30),
    )
    result = {"status": "ok", "message": "saved"}
    terminal = await repository.checkpoint(
        replace(scenario, phase="confirmed"),
        "booking_confirmed_for_telegram",
        action_id=action.id,
        result=result,
    )
    clock.advance(hours=1)

    replay = await repository.consume_action(
        action.id, "telegram", "10", "10"
    )

    assert terminal.revision == scenario.revision + 1
    assert replay is not None
    assert replay.consumed_at == datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    assert replay.result == result
    assert isinstance(replay.result, MappingProxyType)
    assert await repository.consume_action(
        action.id, "telegram", "11", "11"
    ) is None


async def test_checkpoint_rolls_back_action_result_event_and_revision_together(
    database, repository, clock
):
    scenario = await repository.start(
        "create", "telegram", "10", "10", "start:rollback"
    )
    action = await repository.issue_action(
        scenario.id,
        scenario.revision,
        "confirm",
        {},
        clock.now() + timedelta(minutes=30),
    )
    async with database.acquire() as connection:
        await connection.execute(
            """
            CREATE FUNCTION reject_workflow_checkpoint() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.event_type = 'forced_failure' THEN
                    RAISE EXCEPTION 'forced workflow checkpoint failure';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER reject_workflow_checkpoint_insert
            BEFORE INSERT ON booking_events
            FOR EACH ROW EXECUTE FUNCTION reject_workflow_checkpoint();
            """
        )

    with pytest.raises(
        asyncpg.PostgresError, match="forced workflow checkpoint failure"
    ):
        await repository.checkpoint(
            replace(scenario, phase="confirmed"),
            "forced_failure",
            action_id=action.id,
            result={"status": "ok"},
        )

    stored = await repository.get_active("telegram", "10", "10")
    fresh_action = await repository.consume_action(
        action.id, "telegram", "10", "10"
    )
    assert stored == scenario
    assert fresh_action == action


async def test_checkpoint_rejects_action_identity_drift(
    database, repository, clock
):
    scenario = await repository.start(
        "create", "telegram", "10", "10", "start:identity-drift"
    )
    action = await repository.issue_action(
        scenario.id,
        scenario.revision,
        "confirm",
        {},
        clock.now() + timedelta(minutes=30),
    )
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE booking_actions SET customer_id = '11' WHERE id = $1",
            action.id,
        )

    with pytest.raises(RuntimeError, match="^booking action conflict$"):
        await repository.checkpoint(
            replace(scenario, phase="confirmed"),
            "must_not_commit",
            action_id=action.id,
            result={"status": "ok"},
        )

    assert (await repository.get_active("telegram", "10", "10")) == scenario


async def test_only_one_active_scenario_per_telegram_owner(repository):
    first, second = await asyncio.gather(
        repository.start("create", "telegram", "10", "10", "start:1"),
        repository.start("create", "telegram", "10", "10", "start:1"),
    )

    assert second.id == first.id


async def test_stale_concurrent_checkpoint_is_rejected(repository):
    scenario = await repository.start(
        "create", "telegram", "10", "10", "start:revision"
    )

    outcomes = await asyncio.gather(
        repository.checkpoint(
            replace(scenario, state={"winner": "a"}), "checkpoint_a"
        ),
        repository.checkpoint(
            replace(scenario, state={"winner": "b"}), "checkpoint_b"
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    errors = [item for item in outcomes if isinstance(item, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "workflow revision conflict"


class CountingPort:
    def __init__(self, clock: Clock) -> None:
        self.create_calls = 0
        self.slot = Slot(
            id="slot-1",
            service_ids=("service-1",),
            staff_id="staff-1",
            starts_at=clock.now() + timedelta(days=1),
            duration_minutes=60,
        )

    async def list_slots(self, query):
        return [self.slot]

    async def create_booking(self, request):
        self.create_calls += 1
        await asyncio.sleep(0.01)
        return ExternalBooking(
            external_id="external-1",
            customer_id=request.customer_id,
            booking_key=request.booking_key,
            slot_id=request.slot_id,
            service_ids=self.slot.service_ids,
            staff_id=self.slot.staff_id,
            starts_at=self.slot.starts_at,
            status="confirmed",
        )


async def test_concurrent_callback_has_one_port_effect_and_same_saved_result(
    database, repository, clock
):
    initial = await repository.start(
        "create", "telegram", "10", "10", "start:confirm"
    )
    awaiting = await repository.checkpoint(
        replace(
            initial,
            phase="awaiting_confirmation",
            state={
                "customer_name": "Мария",
                "customer_phone": "+70000000000",
                "personal_data_processing_allowed": True,
                "selected_slot_id": "slot-1",
                "slot_query": {
                    "service_ids": ["service-1"],
                    "starts_after": clock.now().isoformat(),
                    "starts_before": (clock.now() + timedelta(days=14)).isoformat(),
                    "staff_id": "staff-1",
                },
            },
        ),
        "booking_confirmation_ready",
    )
    action = await repository.issue_action(
        awaiting.id,
        awaiting.revision,
        "confirm",
        {},
        clock.now() + timedelta(minutes=30),
    )
    port = CountingPort(clock)
    service = BookingService(
        port,
        BookingRepository(database),
        now=clock.now,
    )

    async def callback():
        consumed = await repository.consume_action(
            action.id, "telegram", "10", "10"
        )
        assert consumed is not None
        if consumed.result is not None:
            return consumed.result
        service_result = await service.handle(awaiting.id, confirmed=True)
        saved = {
            "status": service_result.status,
            "message": service_result.message,
        }
        terminal = replace(
            awaiting,
            phase="confirmed",
            state={
                **dict(awaiting.state),
                "external_id": "external-1",
                "starts_at": port.slot.starts_at.isoformat(),
                "status": "confirmed",
            },
        )
        await repository.checkpoint(
            terminal,
            "booking_callback_completed",
            action_id=action.id,
            result=saved,
        )
        replay = await repository.consume_action(
            action.id, "telegram", "10", "10"
        )
        assert replay is not None
        return replay.result

    first, second = await asyncio.gather(callback(), callback())

    assert port.create_calls == 1
    assert first == second == {
        "status": "ok",
        "message": f"Запись подтверждена на {port.slot.starts_at.isoformat()}.",
    }


async def test_owned_active_bookings_and_human_mode_are_owner_bound(
    database, repository, clock
):
    scenario = await repository.start(
        "create", "telegram", "20", "20", "start:owned"
    )
    booking_key = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO bookings
                (id, last_scenario_id, external_id, customer_id, booking_key,
                 slot_id, starts_at, status, snapshot)
            VALUES ($1, $2, 'external-owned', '20', $3, 'slot-owned', $4,
                    'confirmed', $5::jsonb)
            """,
            uuid4(),
            scenario.id,
            booking_key,
            clock.now() + timedelta(days=1),
            '{"service_ids":["service-1"],"staff_id":"staff-1"}',
        )
        await connection.execute(
            """
            INSERT INTO human_mode
                (customer_id, enabled, reason_code, enabled_at, expires_at)
            VALUES ('20', true, 'operator', $1, $2)
            """,
            clock.now(),
            clock.now() + timedelta(minutes=30),
        )

    assert await repository.list_owned_active_bookings("10") == []
    owned = await repository.list_owned_active_bookings("20")
    assert len(owned) == 1
    assert owned[0].external_id == "external-owned"
    assert owned[0].booking_key == booking_key
    assert await repository.is_human_mode("20") is True
    assert await repository.is_human_mode("10") is False
    clock.advance(hours=1)
    assert await repository.is_human_mode("20") is False


async def test_action_id_is_short_opaque_and_payload_is_frozen(repository, clock):
    scenario = await repository.start(
        "create", "telegram", "10", "10", "start:opaque"
    )
    action = await repository.issue_action(
        scenario.id,
        scenario.revision,
        "choose_booking",
        {"provider_booking_id": "provider-secret-42"},
        clock.now() + timedelta(minutes=30),
    )

    assert len(action.id) <= 32
    assert action.action_kind not in action.id
    assert "provider" not in action.id
    assert action.expires_at == clock.now() + timedelta(minutes=30)
    assert isinstance(action.payload, MappingProxyType)
    assert action.payload["provider_booking_id"] == "provider-secret-42"


async def test_action_id_collision_retries_without_aborting_transaction(
    database, repository, clock, monkeypatch
):
    scenario = await repository.start(
        "create", "telegram", "10", "10", "start:collision"
    )
    expires_at = clock.now() + timedelta(minutes=30)
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO booking_actions
                (id, scenario_id, customer_id, channel, chat_id, revision,
                 action_kind, payload, expires_at)
            VALUES ('collision', $1, '10', 'telegram', '10', $2,
                    'old', '{}'::jsonb, $3)
            """,
            scenario.id,
            scenario.revision,
            expires_at,
        )
    candidates = iter(("collision", "fresh-token"))
    monkeypatch.setattr(
        "moroz.booking.workflow_repository.secrets.token_urlsafe",
        lambda _size: next(candidates),
    )

    action = await repository.issue_action(
        scenario.id,
        scenario.revision,
        "choose_service",
        {},
        expires_at,
    )

    assert action.id == "fresh-token"
