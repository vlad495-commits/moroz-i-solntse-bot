from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from moroz.booking.models import Slot
from moroz.booking.yclients_catalog import CatalogRecord
from moroz.messaging.router import RouteDecision
from tests.e2e.booking.telegram_helpers import (
    button_labels as _button_labels,
    coordinator as _coordinator,
    handle as _handle,
)


MOSCOW = ZoneInfo("Europe/Moscow")
pytestmark = pytest.mark.asyncio


def booking_decision(**overrides) -> RouteDecision:
    values = {
        "route": "booking",
        "confidence": 0.99,
        "action": "create",
        "services": ("Криокапсула",),
        "date": "2026-09-05",
    }
    values.update(overrides)
    return RouteDecision(**values)


async def test_free_text_time_window_filters_real_slots(migrated_database_url):
    slots = [
        Slot(f"slot-{hour}", ("331",), "10", datetime(2026, 9, 5, hour, tzinfo=MOSCOW), 60)
        for hour in (17, 18, 19)
    ]
    database, bookings, adapter, coordinator = await _coordinator(
        migrated_database_url,
        slots=slots,
    )
    try:
        reply = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="free-after-18",
            text="Запишите на криокапсулу 5 сентября после 18:00",
            kind="text",
            data={},
            decision=booking_decision(time_from="18:00"),
        )

        assert _button_labels(reply) == ["18:00", "19:00"]
        draft = await bookings.get_active_for_customer("42")
        assert draft.state["date"] == "2026-09-05"
        assert draft.state["time_from"] == "18:00"
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_multiple_services_offer_only_matching_short_choice(migrated_database_url):
    records = tuple(
        CatalogRecord(str(index), "10", name, "Тело", "Анна", 1000, 1000, 30)
        for index, name in (
            (331, "Криокапсула"),
            (332, "Прессотерапия"),
            (333, "Массаж спины"),
            (334, "Водородотерапия"),
        )
    )
    database, bookings, adapter, coordinator = await _coordinator(
        migrated_database_url,
        catalog_records=records,
    )
    try:
        reply = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="two-services",
            text="Криокапсула или прессотерапия 5 сентября",
            kind="text",
            data={},
            decision=booking_decision(
                action="clarify",
                services=("Криокапсула", "Прессотерапия"),
            ),
        )

        assert _button_labels(reply) == ["Криокапсула", "Прессотерапия"]
        assert len(_button_labels(reply)) <= 3
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_question_during_draft_is_not_swallowed_or_reset(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="draft-start",
            text="Хочу криокапсулу 5 сентября",
            kind="text",
            data={},
            decision=booking_decision(),
        )
        before = await bookings.get_active_for_customer("42")

        reply = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="draft-question",
            text="А сколько это стоит?",
            kind="text",
            data={},
            decision=RouteDecision(
                "consultation", 0.99, topics=("price",), services=("Криокапсула",)
            ),
        )

        after = await bookings.get_active_for_customer("42")
        assert reply is None
        assert after.id == before.id
        assert after.state == before.state
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_date_correction_with_no_slots_invalidates_old_callback(migrated_database_url):
    slots = [
        Slot("old-slot", ("331",), "10", datetime(2026, 9, 5, 18, tzinfo=MOSCOW), 60)
    ]
    database, bookings, adapter, coordinator = await _coordinator(
        migrated_database_url,
        slots=slots,
    )
    base = {"customer_id": "42", "user_id": "7", "text": ""}
    try:
        offered = await _handle(
            coordinator,
            database,
            **base,
            update_id="old-date",
            kind="text",
            data={},
            decision=booking_decision(),
        )
        old_callback = offered.delivery_options["reply_markup"]["inline_keyboard"][0][0][
            "callback_data"
        ]

        unavailable = await _handle(
            coordinator,
            database,
            **{**base, "text": "Лучше 6 сентября"},
            update_id="new-date",
            kind="text",
            data={},
            decision=RouteDecision(
                "booking", 0.99, "continue", date="2026-09-06"
            ),
        )
        assert "не нашлось" in unavailable.text

        stale = await _handle(
            coordinator,
            database,
            **base,
            update_id="old-slot-callback",
            kind="callback",
            data={"callback_data": old_callback},
        )
        draft = await bookings.get_active_for_customer("42")
        assert "неактуальна" in stale.text
        assert draft.state["date"] == "2026-09-06"
        assert "choices" not in draft.state
        assert "slot_query" not in draft.state
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_legacy_catalog_callback_is_stale_without_reply_menu(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        reply = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="legacy-callback",
            text="",
            kind="callback",
            data={"callback_data": f"booking:v1:{'0' * 32}:9:0.deadbeef"},
        )

        assert "напишите" in reply.text.casefold()
        assert "keyboard" not in reply.delivery_options.get("reply_markup", {})
        assert await bookings.get_active_for_customer("42") is None
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_create_mutates_only_after_current_confirmation(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = {"customer_id": "42", "user_id": "7", "text": ""}
    try:
        slots = await _handle(
            coordinator,
            database,
            **base,
            update_id="create-start",
            kind="text",
            data={},
            decision=booking_decision(),
        )
        slot_callback = slots.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        contact_request = await _handle(
            coordinator,
            database,
            **base,
            update_id="choose-slot",
            kind="callback",
            data={"callback_data": slot_callback},
        )
        assert contact_request.delivery_options["reply_markup"]["keyboard"][0][0]["request_contact"] is True
        assert adapter.create_calls == 0

        confirmation = await _handle(
            coordinator,
            database,
            **base,
            update_id="send-contact",
            kind="contact",
            data={
                "contact_user_id": "7",
                "phone_number": "+7 900 111-22-33",
                "first_name": "Иван",
                "last_name": "",
            },
        )
        confirm_callback = confirmation.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        assert adapter.create_calls == 0

        result = await _handle(
            coordinator,
            database,
            **base,
            update_id="confirm-create",
            kind="callback",
            data={"callback_data": confirm_callback},
        )

        assert "подтверждена" in result.text.casefold()
        assert adapter.create_calls == 1

        replay = await _handle(
            coordinator,
            database,
            **base,
            update_id="confirm-create-replay",
            kind="callback",
            data={"callback_data": confirm_callback},
        )
        assert replay.text == ""
        assert adapter.create_calls == 1
    finally:
        await database.close()


async def _create_owned_booking(coordinator, database):
    base = {"customer_id": "42", "user_id": "7", "text": ""}
    offered = await _handle(
        coordinator,
        database,
        **base,
        update_id="owned-create",
        kind="text",
        data={},
        decision=booking_decision(),
    )
    selected = await _handle(
        coordinator,
        database,
        **base,
        update_id="owned-slot",
        kind="callback",
        data={
            "callback_data": offered.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        },
    )
    assert selected.delivery_options["reply_markup"]["keyboard"]
    confirmation = await _handle(
        coordinator,
        database,
        **base,
        update_id="owned-contact",
        kind="contact",
        data={
            "contact_user_id": "7",
            "phone_number": "+79001112233",
            "first_name": "Иван",
        },
    )
    await _handle(
        coordinator,
        database,
        **base,
        update_id="owned-confirm",
        kind="callback",
        data={
            "callback_data": confirmation.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        },
    )


async def test_cancel_only_owned_booking_requires_signed_confirmation(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = {"customer_id": "42", "user_id": "7", "text": ""}
    try:
        await _create_owned_booking(coordinator, database)
        confirmation = await _handle(
            coordinator,
            database,
            **base,
            update_id="cancel-owned",
            kind="text",
            data={},
            decision=RouteDecision("booking_management", 0.99, "cancel"),
        )
        assert _button_labels(confirmation) == ["Да, отменить"]
        assert adapter.cancel_calls == 0

        await _handle(
            coordinator,
            database,
            **base,
            update_id="cancel-confirm",
            kind="callback",
            data={
                "callback_data": confirmation.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            },
        )
        assert adapter.cancel_calls == 1

        stranger = await _handle(
            coordinator,
            database,
            customer_id="99",
            user_id="7",
            text="",
            update_id="stranger-view",
            kind="text",
            data={},
            decision=RouteDecision("booking_management", 0.99, "view"),
        )
        assert "только" in stranger.text.casefold()
    finally:
        await database.close()


async def test_reschedule_uses_new_date_and_confirmation(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = {"customer_id": "42", "user_id": "7", "text": ""}
    try:
        await _create_owned_booking(coordinator, database)
        offered = await _handle(
            coordinator,
            database,
            **base,
            update_id="reschedule-owned",
            kind="text",
            data={},
            decision=RouteDecision(
                "booking_management",
                0.99,
                "reschedule",
                date="2026-09-06",
            ),
        )
        assert _button_labels(offered) == ["14:00"]
        confirmation = await _handle(
            coordinator,
            database,
            **base,
            update_id="reschedule-slot",
            kind="callback",
            data={
                "callback_data": offered.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            },
        )
        assert _button_labels(confirmation) == ["Да, перенести"]
        assert adapter.reschedule_calls == 0
        await _handle(
            coordinator,
            database,
            **base,
            update_id="reschedule-confirm",
            kind="callback",
            data={
                "callback_data": confirmation.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            },
        )
        assert adapter.reschedule_calls == 1
    finally:
        await database.close()


async def test_management_followup_keeps_new_date_and_time(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = {"customer_id": "42", "user_id": "7", "text": ""}
    try:
        await _create_owned_booking(coordinator, database)
        selected = await _handle(
            coordinator,
            database,
            **base,
            update_id="manage-view",
            kind="text",
            data={},
            decision=RouteDecision("booking_management", 0.99, "view"),
        )
        assert _button_labels(selected) == ["Перенести", "Отменить"]

        offered = await _handle(
            coordinator,
            database,
            **{**base, "text": "Перенесите на 6 сентября после 14:00"},
            update_id="manage-followup",
            kind="text",
            data={},
            decision=RouteDecision(
                "booking_management",
                0.99,
                "reschedule",
                date="2026-09-06",
                time_from="14:00",
            ),
        )

        assert _button_labels(offered) == ["14:00"]
        draft = await bookings.get_active_for_customer("42")
        assert draft.kind == "reschedule"
        assert draft.state["date"] == "2026-09-06"
        assert draft.state["time_from"] == "14:00"
        assert adapter.reschedule_calls == 0
    finally:
        await database.close()


async def test_any_staff_preference_does_not_loop_on_staff_question(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        offered = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="any-staff",
            text="Криокапсула 5 сентября, специалист без разницы",
            kind="text",
            data={},
            decision=booking_decision(staff="без разницы"),
        )

        assert _button_labels(offered) == ["13:00"]
        draft = await bookings.get_active_for_customer("42")
        assert draft.state["staff_id"] is None
        assert draft.state["staff_name"] == "Любой специалист"
    finally:
        await database.close()


async def test_manual_phone_then_name_reaches_confirmation(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    base = {"customer_id": "42", "user_id": "7", "text": ""}
    try:
        offered = await _handle(
            coordinator,
            database,
            **base,
            update_id="manual-start",
            kind="text",
            data={},
            decision=booking_decision(),
        )
        contact = await _handle(
            coordinator,
            database,
            **base,
            update_id="manual-slot",
            kind="callback",
            data={
                "callback_data": offered.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            },
        )
        assert contact.delivery_options["reply_markup"]["keyboard"]

        name = await _handle(
            coordinator,
            database,
            **{**base, "text": "+7 900 111-22-33"},
            update_id="manual-phone",
            kind="text",
            data={},
            decision=RouteDecision("booking", 0.99, "continue"),
        )
        assert name.text == "Как вас зовут?"
        confirmation = await _handle(
            coordinator,
            database,
            **{**base, "text": "Иван"},
            update_id="manual-name",
            kind="text",
            data={},
            decision=RouteDecision("booking", 0.99, "continue"),
        )

        assert _button_labels(confirmation) == ["Подтвердить"]
        assert "+7******2233" in confirmation.text
        assert adapter.create_calls == 0
    finally:
        await database.close()


async def test_cancel_draft_closes_only_unfinished_flow(migrated_database_url):
    database, bookings, adapter, coordinator = await _coordinator(migrated_database_url)
    try:
        await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="cancel-draft-start",
            text="Хочу записаться",
            kind="text",
            data={},
            decision=booking_decision(),
        )
        reply = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="cancel-draft",
            text="Не хочу продолжать оформление",
            kind="text",
            data={},
            decision=RouteDecision("booking", 0.99, "cancel_draft"),
        )

        assert reply.text == "Текущее действие отменено."
        assert await bookings.get_active_for_customer("42") is None
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_walk_in_service_never_calls_booking_provider(migrated_database_url):
    records = (
        CatalogRecord("1", "10", "Солярий | 10 минут", "Загар", "Анна", 100, 1000, 10),
    )
    database, bookings, adapter, coordinator = await _coordinator(
        migrated_database_url,
        catalog_records=records,
    )
    try:
        reply = await _handle(
            coordinator,
            database,
            customer_id="42",
            user_id="7",
            update_id="walk-in",
            text="Хочу в солярий на 10 минут",
            kind="text",
            data={},
            decision=booking_decision(
                services=("Солярий 10 минут",),
                date=None,
            ),
        )

        assert "запись не нужна" in reply.text.casefold()
        assert await bookings.get_active_for_customer("42") is None
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 0, 0)
    finally:
        await database.close()
