from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from moroz.booking.catalog import CatalogRepository
from moroz.booking.mock_yclients import MockYclientsAdapter
from moroz.booking.models import (
    BookingScenario,
    CancelBooking,
    CreateBooking,
    RescheduleBooking,
    Slot,
    SlotQuery,
)
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


async def _coordinator(migrated_database_url, *, catalog_records=None):
    database = Database(migrated_database_url, min_size=1, max_size=3)
    await database.connect()
    repository = BookingRepository(database, schedule_notifications=False)
    catalog = CatalogRepository(database)
    async with catalog.serialized() as connection:
        await catalog.replace(
            connection,
            CatalogSnapshot(
                catalog_records or (
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


def _button_labels(reply):
    return [
        button["text"]
        for row in reply.delivery_options["reply_markup"]["inline_keyboard"]
        for button in row
    ]


async def test_walk_in_services_are_grouped_and_never_call_booking_adapter(
    migrated_database_url,
):
    records = tuple(
        CatalogRecord(str(index), "10", name, "Загар", "Анна", 100, 500, minutes)
        for index, name, minutes in (
            (1, "Солярий | 1 минута", 1),
            (2, "Солярий | 5 минут", 5),
            (3, "Коллариум 3 минуты", 3),
            (4, "Коллариум 5 минут", 5),
            (5, "КОЛЛАГЕНАРИЙ 1 минута", 1),
            (6, "КОЛЛАГЕНАРИЙ 5 минут", 5),
            (7, "Криокапсула", 2),
        )
    )
    database, repository, adapter, coordinator = await _coordinator(
        migrated_database_url, catalog_records=records
    )
    try:
        expected = ["Коллагенарий", "Коллариум", "Солярий", "Криокапсула"]
        for offset, expected_index in enumerate(range(3)):
            customer_id = str(100 + offset)
            reply = await _handle(
                coordinator,
                database,
                customer_id=customer_id,
                user_id="7",
                update_id=f"walk-in-start-{offset}",
                text="Записаться",
                kind="text",
                data={},
            )
            assert reply.text == "Выберите услугу"
            scenario = await repository.get_active_for_customer(customer_id)
            assert [choice["label"] for choice in scenario.state["choices"]] == expected

            callback = f"booking:v1:{scenario.id.hex}:service:{expected_index}"
            walk_in = await _handle(
                coordinator,
                database,
                customer_id=customer_id,
                user_id="7",
                update_id=f"walk-in-select-{offset}",
                text="",
                kind="callback",
                data={"callback_data": callback},
            )

            assert "предварительная запись не нужна" in walk_in.text.casefold()
            assert "10:00 до 21:00" in walk_in.text
            assert _button_labels(walk_in) == expected
            stored = await repository.get_scenario(scenario.id)
            assert (stored.phase, stored.error_code, stored.state["step"]) == (
                "collecting",
                None,
                "service",
            )
            assert (await repository.get_active_for_customer(customer_id)).id == scenario.id

            if offset == 0:
                other_service = await _handle(
                    coordinator,
                    database,
                    customer_id=customer_id,
                    user_id="7",
                    update_id="walk-in-original-other-service",
                    text="",
                    kind="callback",
                    data={
                        "callback_data": (
                            f"booking:v1:{scenario.id.hex}:service:3"
                        )
                    },
                )
                assert other_service.text == "Выберите специалиста"
                assert "неактуальна" not in other_service.text.casefold()
        assert (
            adapter.list_calls,
            adapter.create_calls,
            adapter.reschedule_calls,
            adapter.cancel_calls,
        ) == (0, 0, 0, 0)
    finally:
        await database.close()


async def test_stale_callback_opens_fresh_service_list(
    migrated_database_url,
):
    database, repository, adapter, coordinator = await _coordinator(
        migrated_database_url
    )
    try:
        reply = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="stale-service",
            text="",
            kind="callback",
            data={
                "callback_data": (
                    f"booking:v1:{'0' * 32}:service:0"
                )
            },
        )

        assert reply.text == "Выберите услугу"
        assert _button_labels(reply) == ["Криокапсула"]
        assert (await repository.get_active_for_customer("42")).state[
            "step"
        ] == "service"
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (
            0,
            0,
            0,
        )
    finally:
        await database.close()


async def test_persistent_menu_restarts_or_leaves_unfinished_booking_flow(
    migrated_database_url,
):
    database, repository, adapter, coordinator = await _coordinator(
        migrated_database_url
    )
    base = {
        "customer_id": "42",
        "user_id": "7",
        "kind": "text",
        "data": {},
    }
    try:
        first = await _handle(
            coordinator,
            database,
            **{**base, "update_id": "menu-start", "text": "Записаться"},
        )
        assert first.text == "Выберите услугу"
        original = await repository.get_active_for_customer("42")

        restarted = await _handle(
            coordinator,
            database,
            **{
                **base,
                "update_id": "menu-restart",
                "text": "📅 Записаться",
            },
        )
        current = await repository.get_active_for_customer("42")
        assert restarted.text == "Выберите услугу"
        assert current.id != original.id
        assert (await repository.get_scenario(original.id)).error_code == (
            "menu_navigation"
        )

        routed = await _handle(
            coordinator,
            database,
            **{
                **base,
                "update_id": "menu-services",
                "text": "✨ Услуги и цены",
            },
        )
        assert routed is None
        assert await repository.get_active_for_customer("42") is None
        assert (await repository.get_scenario(current.id)).error_code == (
            "menu_navigation"
        )
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (
            0,
            0,
            0,
        )
    finally:
        await database.close()


@pytest.mark.parametrize(
    ("kind", "step", "action", "state", "button_label"),
    (
        ("create", "confirm", "confirm", {}, "Подтвердить"),
        (
            "reschedule",
            "confirm_change",
            "confirm_change",
            {"new_starts_at": (NOW + timedelta(days=2)).isoformat()},
            "Да, перенести",
        ),
        (
            "cancel",
            "confirm_change",
            "confirm_change",
            {"starts_at": (NOW + timedelta(days=1)).isoformat()},
            "Да, отменить",
        ),
    ),
)
async def test_stale_confirmation_is_rebuilt_and_old_confirm_cannot_execute_after_menu_exit(
    migrated_database_url,
    kind,
    step,
    action,
    state,
    button_label,
):
    database, repository, adapter, coordinator = await _coordinator(
        migrated_database_url
    )
    scenario = BookingScenario(
        id=uuid4(),
        kind=kind,
        phase="awaiting_confirmation",
        idempotency_key=f"telegram:{kind}:confirmation-recovery",
        customer_id="42",
        state={"step": step, **state},
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
    )
    base = {
        "customer_id": "42",
        "user_id": "7",
        "text": "",
        "kind": "callback",
        "data": {},
    }
    try:
        await repository.create_scenario(scenario)
        stale = await _handle(
            coordinator,
            database,
            **{
                **base,
                "update_id": f"stale-{action}",
                "data": {
                    "callback_data": f"booking:v1:{scenario.id.hex}:service:0"
                },
            },
        )
        assert _button_labels(stale) == [button_label]
        if kind == "reschedule":
            assert stale.text.startswith("Перенести запись на")
        elif kind == "cancel":
            assert stale.text.startswith("Отменить запись на")

        routed = await _handle(
            coordinator,
            database,
            **{
                **base,
                "update_id": f"leave-{action}",
                "text": "📍 Адрес и режим",
                "kind": "text",
            },
        )
        assert routed is None

        old_confirm = await _handle(
            coordinator,
            database,
            **{
                **base,
                "update_id": f"old-{action}",
                "data": {
                    "callback_data": f"booking:v1:{scenario.id.hex}:{action}:0"
                },
            },
        )
        assert old_confirm.text == "Выберите услугу"
        assert _button_labels(old_confirm) == ["Криокапсула"]
        assert (adapter.create_calls, adapter.reschedule_calls) == (0, 0)
    finally:
        await database.close()


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
            assert stale.text == "Выберите услугу"
            assert _button_labels(stale)
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
        assert first.delivery_options["reply_markup"]["is_persistent"] is True
        assert adapter.create_calls == 1

        mine = await _handle(
            coordinator,
            database,
            **{**base, "update_id": "109", "text": "Мои записи", "kind": "text"},
        )
        assert "Криокапсула" in mine.text
        management = await repository.get_active_for_customer("42")
        management_token = management.id.hex
        assert management.state["step"] == "booking_action"
        forged = f"booking:v1:{management_token}:booking_action:99"
        assert (await _handle(
            coordinator, database, **{**base, "update_id": "110", "data": {"callback_data": forged}}
        )).text == "Выберите действие"

        reschedule = f"booking:v1:{management_token}:booking_action:0"
        assert (await _handle(
            coordinator, database, **{**base, "update_id": "112", "data": {"callback_data": reschedule}}
        )).text == "Выберите специалиста"
        change = await repository.get_active_for_customer("42")
        change_token = change.id.hex
        change_staff = f"booking:v1:{change_token}:staff:0"
        await _handle(
            coordinator, database, **{**base, "update_id": "113", "data": {"callback_data": change_staff}}
        )
        change_date = f"booking:v1:{change_token}:available_date:0"
        await _handle(
            coordinator, database, **{**base, "update_id": "114", "data": {"callback_data": change_date}}
        )
        change_slot = f"booking:v1:{change_token}:slot:0"
        change_summary = await _handle(
            coordinator, database, **{**base, "update_id": "115", "data": {"callback_data": change_slot}}
        )
        assert "Перенести запись" in change_summary.text
        assert adapter.reschedule_calls == 0
        change_confirm = f"booking:v1:{change_token}:confirm_change:0"
        moved = await _handle(
            coordinator, database, **{**base, "update_id": "116", "data": {"callback_data": change_confirm}}
        )
        repeated_move = await _handle(
            coordinator, database, **{**base, "update_id": "117", "data": {"callback_data": change_confirm}}
        )
        assert moved.text == repeated_move.text
        assert adapter.reschedule_calls == 1

        mine = await _handle(
            coordinator,
            database,
            **{**base, "update_id": "118", "text": "Мои записи", "kind": "text"},
        )
        assert "Криокапсула" in mine.text
        management = await repository.get_active_for_customer("42")
        management_token = management.id.hex
        cancel = f"booking:v1:{management_token}:booking_action:1"
        cancel_summary = await _handle(
            coordinator, database, **{**base, "update_id": "120", "data": {"callback_data": cancel}}
        )
        assert "Отменить запись" in cancel_summary.text
        assert adapter.cancel_calls == 0
        cancel_scenario = await repository.get_active_for_customer("42")
        cancel_confirm = f"booking:v1:{cancel_scenario.id.hex}:confirm_change:0"
        cancelled = await _handle(
            coordinator, database, **{**base, "update_id": "121", "data": {"callback_data": cancel_confirm}}
        )
        repeated_cancel = await _handle(
            coordinator, database, **{**base, "update_id": "122", "data": {"callback_data": cancel_confirm}}
        )
        assert cancelled.text == repeated_cancel.text
        assert adapter.cancel_calls == 1
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


async def test_cancel_action_closes_only_open_draft(migrated_database_url):
    database, repository, adapter, coordinator = await _coordinator(
        migrated_database_url
    )
    try:
        await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="300",
            text="Хочу записаться",
            kind="text",
            data={},
        )
        active = await repository.get_active_for_customer("42")

        reply = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="301",
            text="Отменить действие",
            kind="text",
            data={},
        )

        assert reply.text == "Текущее действие отменено."
        assert reply.delivery_options["reply_markup"]["is_persistent"] is True
        assert (await repository.get_scenario(active.id)).phase == "failed"
        assert await repository.get_active_for_customer("42") is None
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (
            0,
            0,
            0,
        )
    finally:
        await database.close()
