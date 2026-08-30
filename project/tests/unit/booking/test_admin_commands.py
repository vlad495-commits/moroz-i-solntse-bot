from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from uuid import UUID

import pytest

from moroz.booking.admin_commands import (
    ADMIN_BOOKING_CREATE_KIND,
    ADMIN_BOOKING_STATUS_KIND,
    AdminBookingCommandService,
)
from moroz.booking.models import Slot
from moroz.messaging.models import ScenarioResult
from moroz.notifications.models import SchedulerJob


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
JOB_ID = UUID("11111111-1111-1111-1111-111111111111")


def _job(kind: str, payload: dict[str, object]) -> SchedulerJob:
    return SchedulerJob(
        id=JOB_ID,
        kind=kind,
        run_at=NOW,
        payload=MappingProxyType(payload),
        idempotency_key=f"{kind}:{JOB_ID}",
        attempts=0,
        booking_key=None,
        booking_starts_at=None,
    )


class Adapter:
    def __init__(self, slots=()):
        self.slots = list(slots)
        self.queries = []
        self.statuses = []

    async def list_slots(self, query):
        self.queries.append(query)
        return self.slots

    async def set_visit_status(self, external_id, status):
        self.statuses.append((external_id, status))


class BookingRepository:
    def __init__(self):
        self.scenarios = []

    async def create_scenario(self, scenario):
        self.scenarios.append(scenario)
        return scenario.id


class BookingService:
    def __init__(self):
        self.calls = []

    async def handle(self, scenario_id, *, confirmed):
        self.calls.append((scenario_id, confirmed))
        return ScenarioResult("ok", "ok", None, ())


class CommandRepository:
    def __init__(self):
        self.statuses = []

    async def record_status(self, external_id, status):
        self.statuses.append((external_id, status))


class Scheduler:
    def __init__(self):
        self.jobs = []

    async def schedule(self, job):
        self.jobs.append(job)
        return True


@pytest.mark.asyncio
async def test_create_command_selects_exact_slot_and_runs_existing_booking_service():
    start = NOW + timedelta(days=2)
    slot = Slot("slot-1", ("331",), "6544", start, 60)
    adapter = Adapter([slot])
    bookings = BookingRepository()
    booking_service = BookingService()
    scheduler = Scheduler()
    service = AdminBookingCommandService(
        adapter,
        bookings,
        CommandRepository(),
        scheduler,
        booking_service=booking_service,
        clock=lambda: NOW,
    )

    result = await service.handle(
        _job(
            ADMIN_BOOKING_CREATE_KIND,
            {
                "customer_name": "Анна",
                "customer_phone": "+79990000000",
                "service_id": "331",
                "staff_id": "6544",
                "starts_at": start.isoformat(),
                "personal_data_processing_allowed": True,
                "comment": None,
            },
        )
    )

    assert result.status == "sent"
    assert adapter.queries[0].starts_after == start
    assert adapter.queries[0].starts_before == start + timedelta(minutes=1)
    assert bookings.scenarios[0].id == JOB_ID
    assert bookings.scenarios[0].state["selected_slot_id"] == "slot-1"
    assert booking_service.calls == [(JOB_ID, True)]
    assert scheduler.jobs[0].kind == "yclients_booking_projection_sync"
    assert scheduler.jobs[0].idempotency_key.endswith(str(JOB_ID))


@pytest.mark.asyncio
async def test_create_command_skips_when_exact_slot_is_unavailable():
    service = AdminBookingCommandService(
        Adapter(), BookingRepository(), CommandRepository(), Scheduler(), clock=lambda: NOW
    )

    result = await service.handle(
        _job(
            ADMIN_BOOKING_CREATE_KIND,
            {
                "customer_name": "Анна",
                "customer_phone": "+79990000000",
                "service_id": "331",
                "staff_id": "6544",
                "starts_at": (NOW + timedelta(days=2)).isoformat(),
                "personal_data_processing_allowed": True,
                "comment": None,
            },
        )
    )

    assert result.reason == "slot_unavailable"


@pytest.mark.asyncio
async def test_status_command_updates_yclients_and_local_projection_state():
    adapter = Adapter()
    repository = CommandRepository()
    scheduler = Scheduler()
    service = AdminBookingCommandService(
        adapter,
        BookingRepository(),
        repository,
        scheduler,
        clock=lambda: NOW,
    )

    result = await service.handle(
        _job(
            ADMIN_BOOKING_STATUS_KIND,
            {"external_id": "9001", "status": "no_show"},
        )
    )

    assert result.status == "sent"
    assert adapter.statuses == [("9001", "no_show")]
    assert repository.statuses == [("9001", "no_show")]
    assert scheduler.jobs[0].kind == "yclients_booking_projection_sync"


@pytest.mark.asyncio
async def test_unknown_command_and_private_payload_fail_closed():
    service = AdminBookingCommandService(
        Adapter(), BookingRepository(), CommandRepository(), Scheduler(), clock=lambda: NOW
    )

    unknown = await service.handle(_job("private", {}))
    malformed = await service.handle(
        _job(ADMIN_BOOKING_STATUS_KIND, {"external_id": "secret", "status": "private"})
    )

    assert unknown.reason == "unsupported_kind"
    assert malformed.reason == "invalid_admin_booking_payload"
