import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.booking.models import BookingScenario
from moroz.booking.repository import BookingRepository
from moroz.common.db import Database
from moroz.messaging.repository import MessageRepository


pytestmark = pytest.mark.asyncio
pytest_plugins = ("tests.integration.conftest",)
STAFF_CHAT_ID = "900001"


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=4)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def scenario():
    now = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    return BookingScenario(
        id=uuid4(),
        kind="create",
        phase="executing",
        idempotency_key=f"booking:{uuid4()}",
        customer_id="700001",
        state={"phone": "+79991234567", "external_id": "provider-secret"},
        error_code=None,
        created_at=now,
        updated_at=now,
    )


async def _counts(database, scenario_id):
    async with database.acquire() as connection:
        return await connection.fetchrow(
            """
            SELECT
              (SELECT count(*) FROM booking_events
               WHERE scenario_id=$1 AND event_type='admin_attention_required') events,
              (SELECT count(*) FROM escalations
               WHERE source='booking' AND payload->>'scenario_id'=$1::text) escalations,
              (SELECT count(*) FROM human_mode
               WHERE customer_id='700001' AND enabled) human_modes,
              (SELECT count(*) FROM outbound_messages
               WHERE idempotency_key LIKE 'booking_escalation:%') outbounds,
              (SELECT count(*) FROM task_outbox
               WHERE kind='send_outbound') tasks
            """,
            scenario_id,
        )


async def test_booking_escalation_atomically_sets_all_durable_records(
    database, scenario
):
    repository = BookingRepository(database, staff_chat_id=STAFF_CHAT_ID)
    await repository.create_scenario(scenario)

    await repository.escalate(
        replace(scenario, phase="escalated", error_code="booking_outcome_unknown"),
        "booking_outcome_unknown",
        {"provider_payload": "must-not-escape"},
    )

    async with database.acquire() as connection:
        stored = await connection.fetchrow(
            """
            SELECT s.phase, s.error_code, e.id escalation_id, e.reason_code,
                   e.payload, h.enabled, h.escalation_id human_escalation_id
            FROM booking_scenarios s
            JOIN escalations e ON e.payload->>'scenario_id'=s.id::text
            JOIN human_mode h ON h.customer_id=s.customer_id
            WHERE s.id=$1
            """,
            scenario.id,
        )
        outbounds = await connection.fetch(
            """
            SELECT chat_id, text, idempotency_key
            FROM outbound_messages
            ORDER BY idempotency_key
            """
        )
    counts = await _counts(database, scenario.id)
    assert (stored["phase"], stored["error_code"]) == (
        "escalated",
        "booking_outcome_unknown",
    )
    assert stored["reason_code"] == "booking_outcome_unknown"
    assert stored["enabled"] is True
    assert stored["human_escalation_id"] == stored["escalation_id"]
    assert tuple(counts.values()) == (1, 1, 1, 2, 2)
    material = "\n".join(row["text"] for row in outbounds)
    assert str(scenario.id) in material
    assert "booking_outcome_unknown" in material
    assert "+79991234567" not in material
    assert "provider-secret" not in material
    assert "must-not-escape" not in material
    client = next(row for row in outbounds if row["chat_id"] == scenario.customer_id)
    assert "подтвержд" not in client["text"].lower()


async def test_repository_itself_transitions_nonterminal_scenario_to_escalated(
    database, scenario
):
    repository = BookingRepository(database, staff_chat_id=STAFF_CHAT_ID)
    await repository.create_scenario(scenario)

    await repository.escalate(scenario, "booking_outcome_unknown")

    stored = await repository.get_scenario(scenario.id)
    assert (stored.phase, stored.error_code) == (
        "escalated",
        "booking_outcome_unknown",
    )


async def test_booking_escalation_rolls_back_every_record_on_late_failure(
    database, scenario, monkeypatch
):
    repository = BookingRepository(database, staff_chat_id=STAFF_CHAT_ID)
    await repository.create_scenario(scenario)
    original = MessageRepository.enqueue_outbound_in_transaction
    calls = 0

    async def fail_second(self, connection, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("late enqueue failed")
        return await original(self, connection, **kwargs)

    monkeypatch.setattr(
        MessageRepository,
        "enqueue_outbound_in_transaction",
        fail_second,
    )
    with pytest.raises(RuntimeError, match="late enqueue failed"):
        await repository.escalate(
            replace(scenario, phase="escalated"),
            "booking_outcome_unknown",
        )

    stored = await repository.get_scenario(scenario.id)
    assert (stored.phase, stored.error_code) == ("executing", None)
    assert tuple((await _counts(database, scenario.id)).values()) == (0, 0, 0, 0, 0)


async def test_booking_escalation_replay_and_concurrency_do_not_duplicate(
    database, scenario
):
    first = BookingRepository(database, staff_chat_id=STAFF_CHAT_ID)
    second = BookingRepository(database, staff_chat_id=STAFF_CHAT_ID)
    await first.create_scenario(scenario)
    escalated = replace(scenario, phase="escalated")

    await asyncio.gather(
        first.escalate(escalated, "booking_outcome_unknown"),
        second.escalate(escalated, "booking_outcome_unknown"),
    )
    await first.escalate(escalated, "booking_outcome_unknown")

    assert tuple((await _counts(database, scenario.id)).values()) == (1, 1, 1, 2, 2)


async def test_booking_escalation_reuses_existing_open_scenario_escalation(
    database, scenario
):
    repository = BookingRepository(database, staff_chat_id=STAFF_CHAT_ID)
    await repository.create_scenario(scenario)
    existing_id = uuid4()
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO escalations
                (id, source, customer_id, status, reason_code, payload)
            VALUES ($1, 'booking', $2, 'open', 'older_reason', $3::jsonb)
            """,
            existing_id,
            scenario.customer_id,
            json.dumps({"scenario_id": str(scenario.id)}),
        )

    await repository.escalate(
        replace(scenario, phase="escalated"),
        "booking_outcome_unknown",
    )

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT e.id, e.reason_code, h.escalation_id
            FROM escalations e
            JOIN human_mode h ON h.customer_id=e.customer_id
            WHERE e.payload->>'scenario_id'=$1
            """,
            str(scenario.id),
        )
    assert (row["id"], row["reason_code"], row["escalation_id"]) == (
        existing_id,
        "booking_outcome_unknown",
        existing_id,
    )
    assert tuple((await _counts(database, scenario.id)).values()) == (1, 1, 1, 2, 2)


async def test_escalation_does_not_overwrite_confirmed_terminal_scenario(
    database, scenario
):
    repository = BookingRepository(database, staff_chat_id=STAFF_CHAT_ID)
    confirmed = replace(scenario, phase="confirmed")
    await repository.create_scenario(confirmed)

    await repository.escalate(
        replace(confirmed, phase="escalated"),
        "booking_outcome_unknown",
    )

    stored = await repository.get_scenario(scenario.id)
    assert (stored.phase, stored.error_code) == ("confirmed", None)
    assert tuple((await _counts(database, scenario.id)).values()) == (0, 0, 0, 0, 0)
