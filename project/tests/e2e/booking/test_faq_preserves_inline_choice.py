import pytest

from moroz.messaging.router import RouteDecision
from tests.e2e.booking.telegram_helpers import coordinator, handle
from tests.e2e.booking.test_conversational_telegram_booking import _create_owned_booking


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("reschedule", [False, True])
async def test_existing_slot_button_still_works_after_faq(migrated_database_url, reschedule):
    database, repository, adapter, flow = await coordinator(migrated_database_url)
    base = dict(customer_id="42", user_id="7", text="")
    try:
        if reschedule:
            await _create_owned_booking(flow, database)
        decision = (
            RouteDecision("booking_management", .99, "reschedule", date="2026-09-06")
            if reschedule else RouteDecision(
                "booking", .99, "create", services=("Криокапсула",), date="2026-09-05")
        )
        offered = await handle(flow, database, **base, kind="text", data={},
                               update_id="offer", decision=decision)
        button = offered.delivery_options["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        original = await repository.get_active_for_customer("42")
        calls = (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls)
        assert await handle(flow, database, **base, kind="text", data={},
                            update_id="faq", decision=RouteDecision(
                                "consultation", .99, topics=("preparation",))) is None
        assert await repository.get_active_for_customer("42") == original
        reply = await handle(flow, database, **base, kind="callback",
                             update_id="choose", data={"callback_data": button})
        current = await repository.get_active_for_customer("42")
        assert current.id == original.id
        assert current.state["selected_slot_id"]
        assert current.state["step"] == ("confirm_change" if reschedule else "contact")
        assert "reply_markup" in reply.delivery_options
        assert (adapter.create_calls, adapter.reschedule_calls, adapter.cancel_calls) == calls
    finally:
        await database.close()
