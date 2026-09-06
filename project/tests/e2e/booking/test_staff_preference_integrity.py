import pytest

from moroz.messaging.router import RouteDecision
from tests.e2e.booking.telegram_helpers import coordinator, handle


pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("staff", ["Николай", "а"])
@pytest.mark.parametrize("resolved", ["Анна", "любой специалист"])
async def test_date_does_not_discard_unresolved_staff(migrated_database_url, staff, resolved):
    database, repository, adapter, flow = await coordinator(migrated_database_url)
    base = dict(customer_id="42", user_id="7", text="", kind="text", data={})
    try:
        await handle(flow, database, **base, update_id="start", decision=RouteDecision(
            "booking", .99, "create", services=("Криокапсула",), staff=staff))
        original = await repository.get_active_for_customer("42")
        assert original.state["step"] == "staff"
        reply = await handle(flow, database, **base, update_id="date", decision=RouteDecision(
            "booking", .99, "continue", date="2026-09-05", time_from="13:00"))
        current = await repository.get_active_for_customer("42")
        assert current.id == original.id
        assert current.state["step"] == "staff"
        assert current.state["staff_query"] == staff
        assert current.state["date"] == "2026-09-05"
        assert current.state["time_from"] == "13:00"
        assert "Уточните имя" in reply.text
        assert adapter.list_calls == 0
        await handle(flow, database, **base, update_id="staff", decision=RouteDecision(
            "booking", .99, "continue", staff=resolved))
        ready = await repository.get_active_for_customer("42")
        assert ready.state["step"] == "slot"
        assert ready.state["staff_name"] == ("Анна" if resolved == "Анна" else "Любой специалист")
        assert ready.state["slot_query"]["staff_id"] == ("10" if resolved == "Анна" else None)
        assert adapter.list_calls == 1
        assert adapter.create_calls == 0
    finally:
        await database.close()
