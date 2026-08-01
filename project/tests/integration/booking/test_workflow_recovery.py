import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from moroz.booking.catalog import CatalogService, CatalogStaff
from moroz.booking.interaction import BookingOwner, Interaction, WorkflowReply
from moroz.booking.mock_catalog import MockBookingCatalog
from moroz.booking.models import CreateBooking, ExternalBooking, Slot, SlotQuery
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.booking.workflow import BookingWorkflow
from moroz.booking.workflow_repository import BookingWorkflowRepository
from moroz.common.db import Database


pytestmark = pytest.mark.asyncio


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class SequencePort:
    def __init__(self, *snapshots: list[Slot]) -> None:
        self.snapshots = list(snapshots) or [[_slot("slot-1", 2, 7)]]
        self.list_calls = 0
        self.create_calls = 0
        self.on_create: Callable[[], None] | None = None

    async def list_slots(self, query: SlotQuery) -> list[Slot]:
        index = min(self.list_calls, len(self.snapshots) - 1)
        self.list_calls += 1
        return list(self.snapshots[index])

    async def create_booking(self, command: CreateBooking) -> ExternalBooking:
        self.create_calls += 1
        if self.on_create is not None:
            self.on_create()
        selected = next(
            slot
            for snapshot in self.snapshots
            for slot in snapshot
            if slot.id == command.slot_id
        )
        return ExternalBooking(
            external_id="external-1",
            customer_id=command.customer_id,
            booking_key=command.booking_key,
            slot_id=selected.id,
            service_ids=selected.service_ids,
            staff_id=selected.staff_id,
            starts_at=selected.starts_at,
            status="confirmed",
        )


class BarrierWorkflowRepository(BookingWorkflowRepository):
    def __init__(self, database: Database, clock: Clock) -> None:
        super().__init__(database, now=clock.now)
        self._arrivals = 0
        self._gate = asyncio.Event()

    async def consume_action(self, *args, **kwargs):
        action = await super().consume_action(*args, **kwargs)
        if (
            action is not None
            and action.result is None
            and action.action_kind == "select_service"
        ):
            self._arrivals += 1
            if self._arrivals == 2:
                self._gate.set()
            await asyncio.wait_for(self._gate.wait(), timeout=2)
        return action


class ConfirmBarrierWorkflowRepository(BookingWorkflowRepository):
    def __init__(self, database: Database, clock: Clock) -> None:
        super().__init__(database, now=clock.now)
        self._arrivals = 0
        self._gate = asyncio.Event()

    async def get(self, scenario_id):
        session = await super().get(scenario_id)
        if (
            session is not None
            and session.phase == "awaiting_confirmation"
            and session.state.get("step") == "awaiting_confirmation"
        ):
            self._arrivals += 1
            if self._arrivals == 2:
                self._gate.set()
            await asyncio.wait_for(self._gate.wait(), timeout=2)
        return session


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
def owner():
    return BookingOwner("telegram", "10", "10")


def _slot(slot_id: str, day: int, hour: int) -> Slot:
    return Slot(
        slot_id,
        ("service-1",),
        "staff-1",
        datetime(2026, 8, day, hour, tzinfo=UTC),
        30,
    )


def _catalog() -> MockBookingCatalog:
    return MockBookingCatalog(
        services=(CatalogService("service-1", "Крио", 30),),
        staff=(CatalogStaff("staff-1", "Анна", ("service-1",)),),
        service_allowlist=("service-1",),
        staff_allowlist=("staff-1",),
    )


def _workflow(
    database: Database,
    clock: Clock,
    port: SequencePort,
    *,
    repository: BookingWorkflowRepository | None = None,
) -> tuple[BookingWorkflow, BookingWorkflowRepository, BookingService]:
    workflow_repository = repository or BookingWorkflowRepository(
        database,
        now=clock.now,
    )
    service = BookingService(port, BookingRepository(database), now=clock.now)
    return (
        BookingWorkflow(
            _catalog(),
            port,
            workflow_repository,
            service,
            now=clock.now,
            page_size=2,
        ),
        workflow_repository,
        service,
    )


def _buttons(reply: WorkflowReply) -> list[dict[str, object]]:
    return [
        button
        for row in reply.delivery_options.get("reply_markup", {}).get(
            "inline_keyboard", []
        )
        for button in row
    ]


def _callback(reply: WorkflowReply, text: str) -> str:
    return next(
        str(button["callback_data"])
        for button in _buttons(reply)
        if button["text"] == text
    )


async def _press(
    workflow: BookingWorkflow,
    owner: BookingOwner,
    reply: WorkflowReply,
    text: str,
    key: str,
) -> WorkflowReply:
    return await workflow.handle(
        Interaction.callback(owner, key, _callback(reply, text))
    )


async def _ready_summary(
    workflow: BookingWorkflow,
    owner: BookingOwner,
) -> WorkflowReply:
    reply = await workflow.start_create(owner, "start:1")
    reply = await _press(workflow, owner, reply, "Крио", "service:1")
    reply = await _press(workflow, owner, reply, "Готово", "done:1")
    reply = await _press(
        workflow,
        owner,
        reply,
        "Любой мастер",
        "master:1",
    )
    date = str(_buttons(reply)[0]["text"])
    reply = await _press(workflow, owner, reply, date, "date:1")
    slot = str(_buttons(reply)[0]["text"])
    reply = await _press(workflow, owner, reply, slot, "slot:1")
    await workflow.handle(Interaction.text(owner, "name:1", "Мария"))
    return await workflow.handle(
        Interaction.contact(
            owner,
            "contact:1",
            contact_user_id=owner.customer_id,
            phone_number="+70000000000",
            personal_data_processing_allowed=True,
        )
    )


def _opaque_callbacks(reply: WorkflowReply) -> list[str]:
    return [str(button["callback_data"]) for button in _buttons(reply)]


async def test_confirm_accepted_before_ttl_saves_result_after_service_finishes_late(
    database,
    clock,
    owner,
):
    port = SequencePort([_slot("slot-1", 2, 7)])
    workflow, repository, _service = _workflow(database, clock, port)
    summary = await _ready_summary(workflow, owner)
    callback = _callback(summary, "Подтвердить")
    action_id = callback.removeprefix("booking:")
    port.on_create = lambda: clock.advance(minutes=31)

    first = await workflow.handle(
        Interaction.callback(owner, "confirm:late", callback)
    )
    replay = await workflow.handle(
        Interaction.callback(owner, "confirm:replay", callback)
    )
    saved = await repository.consume_action(
        action_id,
        owner.channel,
        owner.chat_id,
        owner.customer_id,
    )
    stored = await BookingRepository(database).get_scenario(saved.scenario_id)

    assert first == replay
    assert first.text == "Запись подтверждена."
    assert saved.result == first.to_result()
    assert stored.phase == "confirmed"
    assert port.create_calls == 1


async def test_crash_after_terminal_checkpoint_recovers_action_after_ttl(
    database,
    clock,
    owner,
):
    port = SequencePort([_slot("slot-1", 2, 7)])
    workflow, repository, service = _workflow(database, clock, port)
    summary = await _ready_summary(workflow, owner)
    callback = _callback(summary, "Подтвердить")
    action_id = callback.removeprefix("booking:")
    accepted = await repository.consume_action(
        action_id,
        owner.channel,
        owner.chat_id,
        owner.customer_id,
    )
    assert accepted is not None

    await service.handle(accepted.scenario_id, confirmed=True)
    clock.advance(minutes=31)
    recovered = await workflow.handle(
        Interaction.callback(owner, "confirm:recovered", callback)
    )
    replay = await workflow.handle(
        Interaction.callback(owner, "confirm:replay", callback)
    )
    saved = await repository.consume_action(
        action_id,
        owner.channel,
        owner.chat_id,
        owner.customer_id,
    )

    assert recovered == replay
    assert recovered.text == "Запись подтверждена."
    assert saved.result == recovered.to_result()
    assert port.create_calls == 1


async def test_expired_confirmation_restart_recovers_fresh_usable_actions(
    database,
    clock,
    owner,
):
    port = SequencePort([_slot("slot-1", 2, 7)])
    workflow, repository, _service = _workflow(database, clock, port)
    summary = await _ready_summary(workflow, owner)
    callback = _callback(summary, "Подтвердить")
    clock.advance(minutes=31)

    expired = await workflow.handle(
        Interaction.callback(owner, "confirm:expired", callback)
    )
    restarted = await workflow.start_create(owner, "start:after-expiry")
    session = await repository.get_active(
        owner.channel,
        owner.chat_id,
        owner.customer_id,
    )

    assert "истёк" in expired.text.casefold()
    assert session.phase == "collecting"
    assert session.expires_at is None
    assert session.state["step"] in {"date", "master"}
    assert _opaque_callbacks(restarted)
    assert all(item.startswith("booking:") for item in _opaque_callbacks(restarted))
    assert port.create_calls == 0


async def test_slot_unavailable_recovers_on_restart_without_second_service_call(
    database,
    clock,
    owner,
):
    selected = _slot("slot-1", 2, 7)
    alternative = _slot("slot-2", 3, 8)
    port = SequencePort([selected], [alternative], [alternative])
    workflow, repository, _service = _workflow(database, clock, port)
    summary = await _ready_summary(workflow, owner)
    callback = _callback(summary, "Подтвердить")
    action_id = callback.removeprefix("booking:")

    unavailable = await workflow.handle(
        Interaction.callback(owner, "confirm:unavailable", callback)
    )
    replay = await workflow.handle(
        Interaction.callback(owner, "confirm:replay", callback)
    )
    restarted = await workflow.start_create(owner, "start:after-race")
    saved = await repository.consume_action(
        action_id,
        owner.channel,
        owner.chat_id,
        owner.customer_id,
    )

    assert unavailable == replay
    assert "подтверждена" not in unavailable.text.casefold()
    assert saved.result == unavailable.to_result()
    assert port.create_calls == 0
    assert _opaque_callbacks(restarted)
    assert any(
        text in {"03.08", "Любой мастер"}
        for text in (str(button["text"]) for button in _buttons(restarted))
    )


async def test_concurrent_same_confirm_slot_unavailable_returns_durable_replay(
    database,
    clock,
    owner,
):
    selected = _slot("slot-1", 2, 7)
    alternative = _slot("slot-2", 3, 8)
    port = SequencePort(
        [selected],
        [alternative],
        [alternative],
        [alternative],
    )
    repository = ConfirmBarrierWorkflowRepository(database, clock)
    workflow, _, _service = _workflow(
        database,
        clock,
        port,
        repository=repository,
    )
    summary = await _ready_summary(workflow, owner)
    callback = _callback(summary, "Подтвердить")
    action_id = callback.removeprefix("booking:")

    concurrent = await asyncio.gather(
        workflow.handle(
            Interaction.callback(owner, "confirm:first", callback)
        ),
        workflow.handle(
            Interaction.callback(owner, "confirm:second", callback)
        ),
        return_exceptions=True,
    )
    replay = await workflow.handle(
        Interaction.callback(owner, "confirm:replay", callback)
    )
    saved = await repository.consume_action(
        action_id,
        owner.channel,
        owner.chat_id,
        owner.customer_id,
    )

    assert not any(isinstance(result, BaseException) for result in concurrent)
    assert concurrent[0] == concurrent[1] == replay
    assert saved.result == replay.to_result()
    assert "подтверждена" not in replay.text.casefold()
    assert port.create_calls == 0
    assert port.list_calls == 4


async def test_concurrent_same_collecting_action_transitions_once_and_fails_closed(
    database,
    clock,
    owner,
):
    port = SequencePort([_slot("slot-1", 2, 7)])
    repository = BarrierWorkflowRepository(database, clock)
    workflow, _, _service = _workflow(
        database,
        clock,
        port,
        repository=repository,
    )
    services = await workflow.start_create(owner, "start:concurrent")
    callback = _callback(services, "Крио")

    results = await asyncio.gather(
        workflow.handle(
            Interaction.callback(owner, "service:first", callback)
        ),
        workflow.handle(
            Interaction.callback(owner, "service:second", callback)
        ),
        return_exceptions=True,
    )
    session = await repository.get_active(
        owner.channel,
        owner.chat_id,
        owner.customer_id,
    )

    assert not any(isinstance(result, BaseException) for result in results)
    assert session.state["selected_service_ids"] == ("service-1",)
    assert sum("истёк" in result.text.casefold() for result in results) == 1
