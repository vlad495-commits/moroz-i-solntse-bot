import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio

from moroz.booking.interaction import IntentVerdict
from moroz.booking.models import Slot
from moroz.common.db import Database
from tests.e2e.booking.test_telegram_create_flow import (
    _click,
    _process,
    _ready_confirmation,
    _runtime,
)


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def database(migrated_database_url):
    value = Database(migrated_database_url, min_size=1, max_size=10)
    await value.connect()
    try:
        yield value
    finally:
        await value.close()


async def test_mock_telegram_create_list_reschedule_list_cancel_lifecycle(database):
    runtime = _runtime(database)
    runtime.port._slots["slot-b"] = Slot(
        "slot-b",
        ("1", "2"),
        "7",
        datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
        60,
    )
    owner = "551"
    summary = await _ready_confirmation(runtime, database, owner, "change")
    created = await _click(
        runtime,
        owner,
        "change-confirm-create",
        summary,
        "Подтвердить",
    )
    assert created.text == "Запись подтверждена."

    listed = await _process(
        runtime,
        owner=owner,
        update_id="change-list-before",
        text="Мои записи",
    )
    assert all(value in listed.text for value in ("Крио", "Массаж", "Анна"))
    assert "02.08.2026 10:00" in listed.text

    selection = await _process(
        runtime,
        owner=owner,
        update_id="change-reschedule",
        text="/reschedule",
    )
    rendered = json.dumps(selection.options, ensure_ascii=False)
    assert "slot-a" not in rendered
    assert "slot-b" not in rendered
    selection_callback = selection.options["reply_markup"]["inline_keyboard"][0][0][
        "callback_data"
    ]

    foreign = await _process(
        runtime,
        owner="552",
        update_id="change-foreign",
        kind="callback",
        data={"callback_data": selection_callback},
    )
    assert "истёк" in foreign.text.casefold()

    reply = await _click(
        runtime,
        owner,
        "change-select-booking",
        selection,
        "02.08",
    )
    reply = await _click(runtime, owner, "reschedule-master", reply, "Любой")
    reply = await _click(runtime, owner, "reschedule-date", reply, "03.08")
    reply = await _click(runtime, owner, "reschedule-slot", reply, "13:00")
    assert all(value in reply.text for value in ("Крио", "Массаж", "Анна"))
    assert "02.08.2026 10:00" in reply.text
    assert "03.08.2026 13:00" in reply.text

    moved = await _click(
        runtime,
        owner,
        "change-confirm-reschedule",
        reply,
        "Подтвердить",
    )
    replayed_move = await _click(
        runtime,
        owner,
        "change-replay-reschedule",
        reply,
        "Подтвердить",
    )
    assert moved.text == replayed_move.text == "Запись перенесена."

    moved_list = await _process(
        runtime,
        owner=owner,
        update_id="change-list-after",
        text="/bookings",
    )
    assert "03.08.2026 13:00" in moved_list.text
    assert "02.08.2026 10:00" not in moved_list.text

    cancel = await _process(
        runtime,
        owner=owner,
        update_id="change-cancel",
        text="/cancel",
    )
    cancel = await _click(
        runtime,
        owner,
        "change-cancel-select",
        cancel,
        "03.08",
    )
    assert "Да, отменить запись" in json.dumps(
        cancel.options,
        ensure_ascii=False,
    )
    cancelled = await _click(
        runtime,
        owner,
        "change-confirm-cancel",
        cancel,
        "Да, отменить запись",
    )
    replayed_cancel = await _click(
        runtime,
        owner,
        "change-replay-cancel",
        cancel,
        "Да, отменить запись",
    )
    assert cancelled.text == replayed_cancel.text == "Запись отменена."

    async with database.acquire() as connection:
        booking = await connection.fetchrow(
            """
            SELECT status, starts_at
            FROM bookings
            WHERE customer_id = $1
            """,
            owner,
        )
        event_counts = await connection.fetch(
            """
            SELECT event_type, count(*) AS count
            FROM booking_events
            WHERE event_type IN ('booking_confirmed', 'booking_cancelled')
            GROUP BY event_type
            """
        )
    assert (booking["status"], booking["starts_at"]) == (
        "cancelled",
        datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
    )
    assert {row["event_type"]: row["count"] for row in event_counts} == {
        "booking_cancelled": 1,
        "booking_confirmed": 2,
    }


async def test_change_router_routes_bypass_consultant_in_worker(database):
    runtime = _runtime(database)
    runtime.router.return_value = IntentVerdict("booking_cancel", 0.95)

    reply = await _process(
        runtime,
        owner="661",
        update_id="change-router-cancel",
        text="Хочу отменить визит",
    )

    assert "нет предстоящих" in reply.text.casefold()
    runtime.consultant.assert_not_awaited()
