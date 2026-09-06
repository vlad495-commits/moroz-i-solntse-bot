from datetime import datetime

import pytest

from moroz.booking.models import Slot
from moroz.booking.telegram import CLARIFY_REPLY, STALE_REPLY
from moroz.booking.time_display import MOSCOW
from moroz.messaging.router import RouteDecision
from tests.e2e.booking.telegram_helpers import coordinator, handle
from tests.e2e.booking.test_conversational_telegram_booking import (
    _create_owned_booking,
    booking_decision,
)


pytestmark = pytest.mark.asyncio
BASE = {"customer_id": "42", "user_id": "7", "text": ""}


def callback(reply):
    return reply.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]


async def send(flow, database, update_id, decision):
    return await handle(flow, database, **BASE, update_id=update_id,
                        kind="text", data={}, decision=decision)


async def click(flow, database, update_id, value):
    return await handle(flow, database, **BASE, update_id=update_id,
                        kind="callback", data={"callback_data": value})


@pytest.mark.parametrize("route", ["booking", "booking_management"])
async def test_continue_keeps_reschedule_and_mutates_original_once(migrated_database_url, route):
    database, repository, adapter, flow = await coordinator(migrated_database_url)
    try:
        await _create_owned_booking(flow, database)
        adapter.create_calls = 0
        await send(flow, database, "start", RouteDecision("booking_management", .99, "reschedule"))
        original = await repository.get_active_for_customer("42")
        assert await send(flow, database, "faq", RouteDecision("consultation", .99, "none")) is None
        assert await repository.get_active_for_customer("42") == original
        decision = RouteDecision(route, .99, "continue", date="2026-09-06")
        offered = await send(flow, database, "date", decision)
        offered = await send(flow, database, "date", decision)
        current = await repository.get_active_for_customer("42")
        assert (current.id, current.kind) == (original.id, "reschedule")
        for key in ("starts_at", "external_id", "booking_key"):
            assert current.state[key] == original.state[key]
        confirmation = await click(flow, database, "slot", callback(offered))
        assert adapter.reschedule_calls == 0
        repeated = await send(flow, database, "date-again", decision)
        assert callback(repeated) == callback(confirmation)
        assert (await repository.get_active_for_customer("42")).phase == "awaiting_confirmation"
        await click(flow, database, "confirm", callback(confirmation))
        await click(flow, database, "confirm", callback(confirmation))
        await click(flow, database, "confirm-again", callback(confirmation))
        completed = await repository.get_scenario(original.id)
        assert completed.phase == "confirmed"
        assert completed.state["previous_starts_at"] == original.state["starts_at"]
        assert completed.state["external_id"] == original.state["external_id"]
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == (0, 1, 0)
    finally:
        await database.close()


@pytest.mark.parametrize("change", [
    {"date": "2026-09-07"},
    {"time_from": "15:00"},
    {"staff": "Мария"},
])
async def test_reschedule_correction_invalidates_confirmation(migrated_database_url, change):
    slots = [
        Slot("original", ("331",), "10", datetime(2026, 9, 5, 13, tzinfo=MOSCOW), 60),
        Slot("first", ("331",), "10", datetime(2026, 9, 6, 14, tzinfo=MOSCOW), 60),
        Slot("later", ("331",), "11", datetime(2026, 9, 6, 15, tzinfo=MOSCOW), 60),
        Slot("next-day", ("331",), "11", datetime(2026, 9, 7, 15, tzinfo=MOSCOW), 60),
    ]
    database, repository, adapter, flow = await coordinator(migrated_database_url, slots=slots)
    try:
        await _create_owned_booking(flow, database)
        adapter.create_calls = 0
        offered = await send(flow, database, "start", RouteDecision(
            "booking_management", .99, "reschedule", date="2026-09-06"))
        confirmation = await click(flow, database, "first-slot", callback(offered))
        original = await repository.get_active_for_customer("42")
        corrected = await send(flow, database, "correct", RouteDecision(
            "booking_management", .99, "continue", **change))
        current = await repository.get_active_for_customer("42")
        assert current.phase == "collecting"
        assert current.id == original.id
        for key in ("starts_at", "external_id", "booking_key"):
            assert current.state[key] == original.state[key]
        assert "selected_slot_id" not in current.state
        assert "new_starts_at" not in current.state
        stale = await click(flow, database, "old-confirm", callback(confirmation))
        assert STALE_REPLY in stale.text
        assert adapter.reschedule_calls == 0
        fresh = await click(flow, database, "new-slot", callback(corrected))
        assert "Да, перенести" in str(fresh.delivery_options)
        pending = await repository.get_active_for_customer("42")
        expected = "next-day" if "date" in change else "later"
        assert pending.state["selected_slot_id"] == expected
        await click(flow, database, "new-confirm", callback(fresh))
        completed = await repository.get_scenario(original.id)
        assert completed.phase == "confirmed"
        assert completed.state["selected_slot_id"] == expected
        assert (adapter.create_calls, adapter.reschedule_calls) == (0, 1)
    finally:
        await database.close()


@pytest.mark.parametrize("route", ["booking", "booking_management"])
async def test_continue_without_active_scenario_only_clarifies(migrated_database_url, route):
    database, repository, adapter, flow = await coordinator(migrated_database_url)
    try:
        reply = await send(flow, database, "idle", RouteDecision(route, .99, "continue"))
        assert reply.text == CLARIFY_REPLY
        assert await repository.get_active_for_customer("42") is None
        assert (adapter.list_calls, adapter.create_calls, adapter.reschedule_calls) == (0, 0, 0)
    finally:
        await database.close()


async def test_explicit_create_switches_from_reschedule(migrated_database_url):
    database, repository, adapter, flow = await coordinator(migrated_database_url)
    try:
        await _create_owned_booking(flow, database)
        await send(flow, database, "start", RouteDecision("booking_management", .99, "reschedule"))
        original = await repository.get_active_for_customer("42")
        await send(flow, database, "new-create", booking_decision(date="2026-09-06"))
        current = await repository.get_active_for_customer("42")
        assert current.kind == "create" and current.id != original.id
        assert (await repository.get_scenario(original.id)).phase == "failed"
        assert adapter.reschedule_calls == 0
    finally:
        await database.close()


async def test_management_continue_preserves_active_create(migrated_database_url):
    database, repository, adapter, flow = await coordinator(migrated_database_url)
    try:
        await send(flow, database, "start", booking_decision(date=None))
        original = await repository.get_active_for_customer("42")
        offered = await send(flow, database, "date", RouteDecision(
            "booking_management", .99, "continue", date="2026-09-05"))
        current = await repository.get_active_for_customer("42")
        assert current is not None
        assert (current.id, current.kind) == (original.id, "create")
        assert current.state["date"] == "2026-09-05"
        assert callback(offered)
        assert adapter.create_calls == 0
    finally:
        await database.close()
