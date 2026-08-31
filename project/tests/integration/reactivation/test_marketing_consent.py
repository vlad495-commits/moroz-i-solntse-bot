from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.common.db import Database
from moroz.security.consent import ConsentService


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
CLAUSE = (
    "Хочу получать в этом боте сообщения об акциях, новостях и "
    "специальных предложениях (включая рекламные)"
)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    value = Database(migrated_database_url, min_size=1, max_size=2)
    await value.connect()
    try:
        yield value
    finally:
        await value.close()


@pytest_asyncio.fixture
async def connection(migrated_database_url):
    value = await asyncpg.connect(migrated_database_url)
    try:
        yield value
    finally:
        await value.close()


async def test_grant_is_event_first_proven_and_duplicate_is_idempotent(
    database, connection
):
    service = ConsentService(database)

    first = await service.grant_marketing(
        channel="telegram",
        user_id="42",
        proof_text=CLAUSE,
        source="telegram_explicit",
        source_event_id="101",
        occurred_at=NOW,
    )
    duplicate = await service.grant_marketing(
        channel="telegram",
        user_id="42",
        proof_text=CLAUSE,
        source="telegram_explicit",
        source_event_id="101",
        occurred_at=NOW,
    )

    assert first == duplicate
    assert first.active is True
    assert first.consent_version == "marketing-v1"
    assert first.proof_text_hash == sha256(CLAUSE.encode()).hexdigest()
    assert first.source == "telegram_explicit"
    assert first.source_event_id == "101"
    assert await connection.fetchval(
        "SELECT count(*) FROM marketing_consent_events"
    ) == 1
    row = await connection.fetchrow(
        "SELECT proof_event_id, proof_text_hash, active FROM marketing_consents"
    )
    assert row["proof_event_id"] is not None
    assert row["proof_text_hash"] == first.proof_text_hash
    assert row["active"] is True


async def test_duplicate_old_grant_cannot_undo_newer_revocation(
    database, connection
):
    service = ConsentService(database)
    grant = dict(
        channel="telegram",
        user_id="42",
        proof_text=CLAUSE,
        source="telegram_explicit",
        source_event_id="101",
        occurred_at=NOW,
    )
    await service.grant_marketing(**grant)
    await service.revoke_marketing(
        channel="telegram",
        user_id="42",
        source="telegram_explicit",
        source_event_id="102",
        occurred_at=NOW,
    )

    state = await service.grant_marketing(**grant)

    assert state.active is False
    assert await connection.fetchval(
        "SELECT count(*) FROM marketing_consent_events"
    ) == 2


async def test_explicit_reopt_in_unsuppresses_then_grants(database, connection):
    service = ConsentService(database)
    await service.suppress_marketing(
        channel="telegram",
        user_id="42",
        reason="user_stop",
        source="telegram_explicit",
        source_event_id="103",
        occurred_at=NOW,
    )

    async with database.acquire() as transaction_connection:
        async with transaction_connection.transaction():
            await service.unsuppress_marketing(
                channel="telegram",
                user_id="42",
                proof_text=CLAUSE,
                source="telegram_explicit",
                source_event_id="104",
                occurred_at=NOW,
                connection=transaction_connection,
            )
            state = await service.grant_marketing(
                channel="telegram",
                user_id="42",
                proof_text=CLAUSE,
                source="telegram_explicit",
                source_event_id="104",
                occurred_at=NOW,
                connection=transaction_connection,
            )

    assert state.active is True
    assert state.suppressed is False
    events = await connection.fetch(
        "SELECT action, created_at FROM marketing_consent_events "
        "WHERE source_event_id = '104' ORDER BY created_at"
    )
    assert [row["action"] for row in events] == ["unsuppressed", "granted"]
    assert events[0]["created_at"] < events[1]["created_at"]


async def test_revoke_and_suppress_cancel_pending_steps(database, connection):
    service = ConsentService(database)
    program_id = uuid4()
    journey_id = uuid4()
    await connection.execute(
        """
        INSERT INTO reactivation_program_versions
            (id, version_number, status, inactivity_days, reminder_enabled,
             reminder_after_days, cooldown_days, main_text, reminder_text,
             template_checksum)
        VALUES ($1, 1, 'draft', 90, true, 5, 90, 'main', 'reminder', 'hash')
        """,
        program_id,
    )
    await connection.execute(
        """
        INSERT INTO reactivation_journeys
            (id, channel, user_id, program_version_id, status, activity_anchor_at)
        VALUES ($1, 'telegram', '42', $2, 'active', $3)
        """,
        journey_id,
        program_id,
        NOW,
    )
    await connection.execute(
        """
        INSERT INTO reactivation_journey_steps
            (id, journey_id, step_kind, status, due_at, idempotency_key)
        VALUES ($1, $2, 'reminder', 'scheduled', $3, 'journey:42:reminder')
        """,
        uuid4(),
        journey_id,
        NOW,
    )

    await service.revoke_marketing(
        channel="telegram",
        user_id="42",
        source="telegram_explicit",
        source_event_id="105",
        occurred_at=NOW,
    )
    await service.suppress_marketing(
        channel="telegram",
        user_id="42",
        reason="user_stop",
        source="telegram_explicit",
        source_event_id="105",
        occurred_at=NOW,
    )

    assert await connection.fetchval(
        "SELECT status FROM reactivation_journey_steps WHERE journey_id = $1",
        journey_id,
    ) == "cancelled"
    state = await service.get_marketing_status("telegram", "42")
    assert (state.active, state.suppressed, state.suppression_reason) == (
        False,
        True,
        "user_stop",
    )


async def test_supplied_connection_rolls_back_event_and_materialized_state(
    database, connection
):
    service = ConsentService(database)

    transaction = connection.transaction()
    await transaction.start()
    try:
        await service.grant_marketing(
            channel="telegram",
            user_id="42",
            proof_text=CLAUSE,
            source="telegram_explicit",
            source_event_id="106",
            occurred_at=NOW,
            connection=connection,
        )
    finally:
        await transaction.rollback()

    assert await connection.fetchval(
        "SELECT count(*) FROM marketing_consent_events"
    ) == 0
    assert await connection.fetchval("SELECT count(*) FROM marketing_consents") == 0


async def test_delayed_lower_telegram_update_is_audited_without_materializing(
    database, connection
):
    service = ConsentService(database)
    stop = {
        "channel": "telegram",
        "user_id": "42",
        "source": "telegram_explicit",
        "source_event_id": "200",
        "occurred_at": NOW,
    }
    await service.revoke_marketing(**stop)
    await service.suppress_marketing(**stop, reason="user_stop")

    delayed_enable = {
        "channel": "telegram",
        "user_id": "42",
        "proof_text": CLAUSE,
        "source": "telegram_explicit",
        "source_event_id": "199",
        "occurred_at": NOW + timedelta(seconds=5),
    }
    await service.unsuppress_marketing(**delayed_enable)
    stale_state = await service.grant_marketing(**delayed_enable)

    assert (
        stale_state.active,
        stale_state.suppressed,
        stale_state.suppression_reason,
    ) == (False, True, "user_stop")
    assert {
        row["action"]
        for row in await connection.fetch(
            "SELECT action FROM marketing_consent_events "
            "WHERE source_event_id = '199'"
        )
    } == {"unsuppressed", "granted"}

    newer_enable = {**delayed_enable, "source_event_id": "201"}
    await service.unsuppress_marketing(**newer_enable)
    current_state = await service.grant_marketing(**newer_enable)

    assert (current_state.active, current_state.suppressed) == (True, False)


async def test_older_non_numeric_event_cannot_override_newer_occurred_at(
    database, connection
):
    service = ConsentService(database)
    await service.grant_marketing(
        channel="telegram",
        user_id="42",
        proof_text=CLAUSE,
        source="telegram_explicit",
        source_event_id="201",
        occurred_at=NOW + timedelta(minutes=1),
    )

    state = await service.revoke_marketing(
        channel="telegram",
        user_id="42",
        source="admin_revoke",
        source_event_id="admin-old",
        occurred_at=NOW,
    )

    assert state.active is True
    assert await connection.fetchval(
        "SELECT count(*) FROM marketing_consent_events "
        "WHERE source_event_id = 'admin-old' AND action = 'revoked'"
    ) == 1


@pytest.mark.parametrize("processing_order", ["grant_first", "safety_first"])
@pytest.mark.parametrize(
    ("safety_occurred_at", "safety_source_event_id"),
    [
        (NOW + timedelta(minutes=1), "admin-later"),
        (NOW, "1"),
    ],
    ids=["newer-nonnumeric", "numeric-looking-non-telegram"],
)
async def test_total_event_order_is_independent_of_processing_order(
    database,
    connection,
    processing_order,
    safety_occurred_at,
    safety_source_event_id,
):
    service = ConsentService(database)

    async def grant():
        await service.grant_marketing(
            channel="telegram",
            user_id="total-order",
            proof_text=CLAUSE,
            source="telegram_explicit",
            source_event_id="9" * 80,
            occurred_at=NOW,
        )

    async def suppress():
        common = {
            "channel": "telegram",
            "user_id": "total-order",
            "source": "admin_revoke",
            "source_event_id": safety_source_event_id,
            "occurred_at": safety_occurred_at,
        }
        await service.revoke_marketing(**common)
        await service.suppress_marketing(**common, reason="admin_revoke")

    actions = (grant, suppress)
    if processing_order == "safety_first":
        actions = tuple(reversed(actions))
    for action in actions:
        await action()

    state = await service.get_marketing_status("telegram", "total-order")
    assert (
        state.active,
        state.suppressed,
        state.suppression_reason,
    ) == (False, True, "admin_revoke")
    materialized = await connection.fetchrow(
        """
        SELECT granted_at, proof_event_id, proof_text_hash
        FROM marketing_consents
        WHERE channel = 'telegram' AND user_id = 'total-order'
        """
    )
    assert tuple(materialized.values()) == (None, None, None)


async def test_trusted_telegram_sequence_supports_eighty_digit_ids(
    database,
):
    service = ConsentService(database)
    lower_id = "9" * 79
    higher_id = "1" + "0" * 79
    stop = {
        "channel": "telegram",
        "user_id": "large-sequence",
        "source": "telegram_explicit",
        "source_event_id": lower_id,
        "occurred_at": NOW,
    }
    await service.revoke_marketing(**stop)
    await service.suppress_marketing(**stop, reason="user_stop")
    enable = {
        **stop,
        "proof_text": CLAUSE,
        "source_event_id": higher_id,
    }
    await service.unsuppress_marketing(**enable)
    state = await service.grant_marketing(**enable)

    assert (state.active, state.suppressed) == (True, False)


async def test_equal_id_grant_pair_converges_when_processed_in_reverse(
    database,
):
    service = ConsentService(database)
    stop = {
        "channel": "telegram",
        "user_id": "reverse-pair",
        "source": "telegram_explicit",
        "source_event_id": "200",
        "occurred_at": NOW,
    }
    await service.revoke_marketing(**stop)
    await service.suppress_marketing(**stop, reason="user_stop")
    enable = {
        **stop,
        "proof_text": CLAUSE,
        "source_event_id": "201",
        "occurred_at": NOW + timedelta(minutes=1),
    }

    await service.grant_marketing(**enable)
    state = await service.unsuppress_marketing(**enable)

    assert (state.active, state.suppressed) == (True, False)


@pytest.mark.parametrize(
    "processing_order",
    [
        ("grant199", "admin", "grant200"),
        ("admin", "grant199", "grant200"),
    ],
)
async def test_new_sequence_rematerializes_actual_durable_winner(
    database,
    connection,
    processing_order,
):
    service = ConsentService(database)

    async def grant(source_event_id, occurred_at):
        await service.grant_marketing(
            channel="telegram",
            user_id="winner-triad",
            proof_text=CLAUSE,
            source="telegram_explicit",
            source_event_id=source_event_id,
            occurred_at=occurred_at,
        )

    async def admin():
        common = {
            "channel": "telegram",
            "user_id": "winner-triad",
            "source": "admin_revoke",
            "source_event_id": "admin-triad",
            "occurred_at": NOW + timedelta(minutes=15),
        }
        await service.revoke_marketing(**common)
        await service.suppress_marketing(**common, reason="admin_revoke")

    actions = {
        "grant199": lambda: grant("199", NOW + timedelta(minutes=20)),
        "grant200": lambda: grant("200", NOW + timedelta(minutes=10)),
        "admin": admin,
    }
    for action in processing_order:
        await actions[action]()

    state = await service.get_marketing_status("telegram", "winner-triad")
    assert (
        state.active,
        state.suppressed,
        state.suppression_reason,
        state.source,
    ) == (False, True, "admin_revoke", "admin_revoke")
    assert await connection.fetchval(
        "SELECT proof_event_id IS NULL AND proof_text_hash IS NULL "
        "FROM marketing_consents WHERE user_id = 'winner-triad'"
    ) is True
