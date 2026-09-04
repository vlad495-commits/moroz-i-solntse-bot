from datetime import UTC, datetime, timedelta

import pytest

from moroz.booking.catalog import CatalogRepository
from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import CreateBooking, Slot, SlotQuery
from moroz.booking.repository import BookingRepository
from moroz.booking.service import BookingService
from moroz.booking.telegram import TelegramBookingCoordinator
from moroz.booking.yclients_catalog import CatalogRecord, CatalogSnapshot
from moroz.common.db import Database


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


class CountingAdapter(MockYclientsAdapter):
    def __init__(self, slots):
        super().__init__(slots)
        self.list_calls = 0
        self.create_calls = 0

    async def list_slots(self, query: SlotQuery):
        self.list_calls += 1
        return await super().list_slots(query)

    async def create_booking(self, command: CreateBooking):
        self.create_calls += 1
        return await super().create_booking(command)


async def _coordinator(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=3)
    await database.connect()
    repository = BookingRepository(database, schedule_notifications=False)
    catalog = CatalogRepository(database)
    async with catalog.serialized() as connection:
        await catalog.replace(
            connection,
            CatalogSnapshot(
                (
                    CatalogRecord(
                        "331", "10", "Криокапсула", "Крио", "Анна",
                        1000, 1000, 60,
                    ),
                    CatalogRecord(
                        "331", "11", "Криокапсула", "Крио", "Мария",
                        1000, 1000, 60,
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
    slots = [
        Slot("signed-slot-1", ("331",), "10", NOW + timedelta(days=1), 60),
        Slot(
            "signed-slot-2",
            ("331",),
            "11",
            NOW + timedelta(days=2, hours=1),
            60,
        ),
    ]
    adapter = CountingAdapter(slots)
    coordinator = TelegramBookingCoordinator(
        repository,
        catalog,
        BookingService(adapter, repository, now=lambda: NOW),
        adapter,
        now=lambda: NOW,
    )
    return database, repository, adapter, coordinator


async def _handle(coordinator, database, **kwargs):
    async with database.acquire() as connection:
        return await coordinator.handle(connection, **kwargs)


async def test_create_booking_flow_uses_server_choices_and_mutates_once(
    migrated_database_url,
):
    database, repository, adapter, coordinator = await _coordinator(
        migrated_database_url
    )
    base = {
        "customer_id": "42",
        "user_id": "7",
        "text": "",
        "kind": "callback",
        "data": {},
    }
    try:
        reply = await _handle(
            coordinator,
            database,
            **{**base, "update_id": "100", "text": "Хочу записаться", "kind": "text"},
        )
        assert reply.text == "Выберите услугу"
        scenario = await repository.get_active_for_customer("42")
        assert scenario.state["choices"][0]["service_id"] == "331"
        token = scenario.id.hex

        for customer_id, callback in (
            ("other", f"booking:v1:{token}:service:0"),
            ("42", f"booking:v1:{'0' * 32}:service:0"),
            ("42", f"booking:v1:{token}:staff:0"),
            ("42", f"booking:v1:{token}:service:99"),
        ):
            stale = await _handle(
                coordinator,
                database,
                **{
                    **base,
                    "customer_id": customer_id,
                    "update_id": callback,
                    "data": {"callback_data": callback},
                },
            )
            assert stale.text == "Эта кнопка уже неактуальна. Начните запись заново."
        assert (adapter.list_calls, adapter.create_calls) == (0, 0)

        service = f"booking:v1:{token}:service:0"
        assert (await _handle(
            coordinator, database, **{**base, "update_id": "101", "data": {"callback_data": service}}
        )).text == "Выберите специалиста"
        staff = f"booking:v1:{token}:staff:0"
        assert (await _handle(
            coordinator, database, **{**base, "update_id": "102", "data": {"callback_data": staff}}
        )).text == "Выберите дату"
        date = f"booking:v1:{token}:available_date:0"
        assert (await _handle(
            coordinator, database, **{**base, "update_id": "103", "data": {"callback_data": date}}
        )).text == "Выберите время"
        slot = f"booking:v1:{token}:slot:0"
        contact_request = await _handle(
            coordinator, database, **{**base, "update_id": "104", "data": {"callback_data": slot}}
        )
        assert contact_request.text.startswith("Отправьте свой контакт")
        assert contact_request.delivery_options["reply_markup"]["keyboard"][0][0]["request_contact"] is True

        foreign = await _handle(
            coordinator,
            database,
            **{
                **base,
                "update_id": "105",
                "kind": "contact",
                "data": {
                    "contact_user_id": "999",
                    "phone_number": "+79991234567",
                    "first_name": "Иван",
                    "last_name": "",
                },
            },
        )
        assert foreign.text == "Пожалуйста, отправьте именно свой контакт."

        summary = await _handle(
            coordinator,
            database,
            **{
                **base,
                "update_id": "106",
                "kind": "contact",
                "data": {
                    "contact_user_id": "7",
                    "phone_number": "+79991234567",
                    "first_name": "Иван",
                    "last_name": "",
                },
            },
        )
        assert "+7******4567" in summary.text
        assert (await repository.get_scenario(scenario.id)).phase == "awaiting_confirmation"
        assert adapter.create_calls == 0

        confirm = f"booking:v1:{token}:confirm:0"
        first = await _handle(
            coordinator, database, **{**base, "update_id": "107", "data": {"callback_data": confirm}}
        )
        repeated = await _handle(
            coordinator, database, **{**base, "update_id": "108", "data": {"callback_data": confirm}}
        )
        assert first.text == repeated.text
        assert "Запись подтверждена" in first.text
        assert adapter.create_calls == 1
    finally:
        await database.close()


async def test_manual_phone_without_name_requests_name(migrated_database_url):
    database, repository, _, coordinator = await _coordinator(migrated_database_url)
    try:
        scenario = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="200",
            text="Хочу записаться",
            kind="text",
            data={},
        )
        active = await repository.get_active_for_customer("42")
        state = dict(active.state)
        state.update({
            "step": "contact",
            "selected_slot_id": "signed-slot-1",
            "slot_query": {
                "service_ids": ["331"],
                "starts_after": NOW.isoformat(),
                "starts_before": (NOW + timedelta(days=14)).isoformat(),
                "staff_id": None,
            },
            "service_name": "Криокапсула",
            "staff_name": "Любой специалист",
            "starts_at": (NOW + timedelta(days=1)).isoformat(),
        })
        from dataclasses import replace
        await repository.checkpoint(replace(active, state=state), "test_contact_step")

        reply = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="201",
            text="8 999 123-45-67",
            kind="text",
            data={},
        )

        assert reply.text == "Как вас зовут?"
        assert (await repository.get_scenario(active.id)).state["step"] == "name"
    finally:
        await database.close()
