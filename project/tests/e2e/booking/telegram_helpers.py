from datetime import UTC, datetime, timedelta

from moroz.booking.catalog import CatalogRepository
from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import CancelBooking, CreateBooking, RescheduleBooking, Slot, SlotQuery
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.booking.telegram import TelegramBookingCoordinator
from moroz.booking.yclients_catalog import CatalogRecord, CatalogSnapshot
from moroz.common.db import Database
from moroz.messaging.router import RouteDecision


NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


class CountingAdapter(MockYclientsAdapter):
    def __init__(self, slots):
        super().__init__(slots)
        self.list_calls = 0
        self.create_calls = 0
        self.reschedule_calls = 0
        self.cancel_calls = 0

    async def list_slots(self, query: SlotQuery):
        self.list_calls += 1
        return await super().list_slots(query)

    async def create_booking(self, command: CreateBooking):
        self.create_calls += 1
        return await super().create_booking(command)

    async def reschedule_booking(self, command: RescheduleBooking):
        self.reschedule_calls += 1
        return await super().reschedule_booking(command)

    async def cancel_booking(self, command: CancelBooking):
        self.cancel_calls += 1
        return await super().cancel_booking(command)


async def coordinator(migrated_database_url, *, catalog_records=None, slots=None):
    database = Database(migrated_database_url, min_size=1, max_size=3)
    await database.connect()
    repository = BookingRepository(database, schedule_notifications=False)
    catalog = CatalogRepository(database)
    async with catalog.serialized() as connection:
        await catalog.replace(
            connection,
            CatalogSnapshot(
                catalog_records
                or (
                    CatalogRecord(
                        "331", "10", "Криокапсула", "Крио", "Анна", 1000, 1000, 60
                    ),
                    CatalogRecord(
                        "331", "11", "Криокапсула", "Крио", "Мария", 1000, 1000, 60
                    ),
                ),
                NOW,
            ),
        )
    async with database.acquire() as connection:
        await connection.execute(
            "INSERT INTO processing_consents "
            "(channel, user_id, consent_version) VALUES ('telegram', '7', 'v1')"
        )
    slots = slots or [
        Slot("signed-slot-1", ("331",), "10", NOW + timedelta(days=1), 60),
        Slot("signed-slot-2", ("331",), "11", NOW + timedelta(days=2, hours=1), 60),
    ]
    adapter = CountingAdapter(slots)
    booking_coordinator = TelegramBookingCoordinator(
        repository,
        catalog,
        BookingService(adapter, repository, now=lambda: NOW),
        adapter,
        now=lambda: NOW,
    )
    return database, repository, adapter, booking_coordinator


async def handle(booking_coordinator, database, **kwargs):
    if kwargs.get("kind") == "callback":
        raw = kwargs.get("data", {}).get("callback_data", "")
        parsed = booking_coordinator._parse_callback(raw)
        if parsed is not None and parsed[3] is None:
            scenario = await booking_coordinator._repository.get_scenario(parsed[0])
            if scenario is not None:
                kwargs["data"] = {
                    "callback_data": booking_coordinator._callback(
                        scenario, parsed[1], parsed[2]
                    )
                }
    if kwargs.get("kind") == "text" and "decision" not in kwargs:
        decision = {
            "Хочу записаться": RouteDecision("booking", 1, "create"),
            "Записаться": RouteDecision("booking", 1, "create"),
            "Мои записи": RouteDecision("booking_management", 1, "view"),
            "Отменить действие": RouteDecision("booking", 1, "cancel_draft"),
            "Иван": RouteDecision("booking", 1, "provide_name"),
        }.get(kwargs.get("text"))
        if decision:
            kwargs["decision"] = decision
    async with database.acquire() as connection:
        return await booking_coordinator.handle(connection, **kwargs)


def button_labels(reply):
    return [
        button["text"]
        for row in reply.delivery_options["reply_markup"]["inline_keyboard"]
        for button in row
    ]
