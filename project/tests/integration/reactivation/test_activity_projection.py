from datetime import UTC, datetime, timedelta
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from moroz.booking.projection import ProjectionRepository
from moroz.booking.yclients_records import ProjectionRecord, ProjectionSnapshot
from moroz.common.db import Database
from moroz.reactivation.activity import (
    ACTIVITY_SOURCE_VERSION,
    ActivityCandidate,
    ActivityRepository,
    ClientActivitySnapshot,
)


pytest_plugins = ["tests.integration.conftest"]
pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    value = Database(migrated_database_url, min_size=2, max_size=2)
    await value.connect()
    try:
        yield value
    finally:
        await value.close()


async def _consent(connection, user_id: str) -> None:
    await connection.execute(
        """
        INSERT INTO marketing_consents
            (id, channel, user_id, consent_version, active, source)
        VALUES ($1, 'telegram', $2, 'marketing-v1', false, 'legacy_unproven')
        """,
        uuid4(),
        user_id,
    )


async def _booking(connection, user_id: str, external_id: str, booking_key) -> None:
    scenario_id = uuid4()
    await connection.execute(
        """
        INSERT INTO booking_scenarios
            (id, kind, phase, idempotency_key, customer_id, state,
             created_at, updated_at)
        VALUES ($1, 'create', 'confirmed', $2, $3, '{}'::jsonb, $4, $4)
        """,
        scenario_id,
        f"activity:{uuid4()}",
        user_id,
        NOW,
    )
    await connection.execute(
        """
        INSERT INTO bookings
            (id, last_scenario_id, external_id, customer_id, slot_id,
             starts_at, scheduled_end_at, status, snapshot, booking_key,
             created_at, updated_at)
        VALUES ($1, $2, $3, $4, 'slot', $5, $6, 'completed',
                '{}'::jsonb, $7, $5, $5)
        """,
        uuid4(),
        scenario_id,
        external_id,
        user_id,
        NOW - timedelta(days=90),
        NOW - timedelta(days=90, hours=-1),
        booking_key,
    )


def _projection_record(
    *,
    external_id: str,
    booking_key,
    client_id: str,
    starts_at: datetime = NOW + timedelta(days=1),
) -> ProjectionRecord:
    return ProjectionRecord(
        external_id=external_id,
        booking_key=booking_key,
        bot_marker_state="valid",
        starts_at=starts_at,
        scheduled_end_at=starts_at + timedelta(hours=1),
        status="confirmed",
        deleted=False,
        client_name="Synthetic",
        staff_name="Synthetic",
        service_names=("Synthetic",),
        client_id=client_id,
        record_created_at=NOW - timedelta(days=100),
    )


async def test_population_claim_and_owned_history_columns_preserve_inbound(database):
    repository = ActivityRepository(database)
    inbound = NOW - timedelta(hours=2)
    old_history = NOW - timedelta(days=2)
    async with database.acquire() as connection:
        await _consent(connection, "42")
        await connection.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, yclients_client_id, identity_status,
                 last_meaningful_inbound_at, history_synced_at,
                 recent_bookings_synced_at, sync_status, created_at, updated_at)
            VALUES ('telegram', '84', '84', 'verified', $1, $2, $3,
                    'current', $3, $3)
            """,
            inbound,
            NOW - timedelta(hours=23, minutes=55),
            NOW,
        )

    async with repository.serialized() as connection:
        await repository.prepare_candidates(connection)
        claimed = await repository.claim_candidates(connection, now=NOW, limit=25)
        assert {(item.user_id, item.identity_status) for item in claimed} == {
            ("42", "unverified"),
            ("84", "verified"),
        }
        await repository.apply_snapshot(
            connection,
            ActivityCandidate("telegram", "84", "verified", "84"),
            ClientActivitySnapshot(
                yclients_client_id="84",
                last_completed_visit_at=NOW - timedelta(days=100),
                next_active_booking_at=NOW + timedelta(days=1),
                history_synced_at=NOW,
                source_version=ACTIVITY_SOURCE_VERSION,
                sync_status="current",
            ),
        )

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT * FROM customer_activity_projection WHERE user_id = '84'"
        )
    assert row["last_meaningful_inbound_at"] == inbound
    assert row["last_completed_visit_at"] == NOW - timedelta(days=100)
    assert row["history_synced_at"] == NOW
    assert row["next_active_booking_at"] is None
    assert row["recent_bookings_synced_at"] == NOW
    assert row["sync_status"] == "current"


async def test_claim_batch_does_not_starve_verified_history_behind_unverified_rows(database):
    repository = ActivityRepository(database)
    old = NOW - timedelta(days=2)
    async with database.acquire() as connection:
        await connection.executemany(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, identity_status, sync_status,
                 created_at, updated_at)
            VALUES ('telegram', $1, 'unverified', 'never', $2, $2)
            """,
            [(f"unverified-{index:02d}", old) for index in range(25)],
        )
        await connection.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, yclients_client_id, identity_status,
                 history_synced_at, sync_status, created_at, updated_at)
            VALUES ('telegram', 'verified-due', '55', 'verified',
                    $1, 'current', $2, $2)
            """,
            NOW - timedelta(hours=23, minutes=55),
            NOW,
        )

    async with repository.serialized() as connection:
        claimed = await repository.claim_candidates(connection, now=NOW, limit=25)

    assert len(claimed) == 25
    assert "verified-due" in {item.user_id for item in claimed}


async def test_current_projection_proof_requires_same_local_owner_and_booking_key(database):
    repository = ActivityRepository(database)
    key = uuid4()
    other_key = uuid4()
    async with database.acquire() as connection:
        for user_id in ("42", "84"):
            await connection.execute(
                """
                INSERT INTO customer_activity_projection
                    (channel, user_id, identity_status, sync_status)
                VALUES ('telegram', $1, 'unverified', 'never')
                """,
                user_id,
            )
        await _booking(connection, "42", "9001", key)
        await _booking(connection, "84", "9002", other_key)
    snapshot = ProjectionSnapshot(
        records=(
            _projection_record(external_id="9001", booking_key=key, client_id="55"),
            _projection_record(external_id="9002", booking_key=key, client_id="66"),
        ),
        synced_at=NOW,
    )
    projection = ProjectionRepository(database)
    async with projection.serialized() as connection:
        await projection.replace(connection, snapshot)

    async with repository.serialized() as connection:
        owner = ActivityCandidate("telegram", "42", "unverified", None)
        other = ActivityCandidate("telegram", "84", "unverified", None)
        assert await repository.current_identity_client_ids(connection, owner) == ("55",)
        assert await repository.current_identity_client_ids(connection, other) == ()


async def test_one_client_claimed_by_two_users_marks_both_conflict_atomically(database):
    repository = ActivityRepository(database)
    async with database.acquire() as connection:
        await connection.executemany(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, yclients_client_id, identity_status,
                 identity_source, identity_verified_at, sync_status)
            VALUES ('telegram', $1, $2, $3, $4, $5, 'never')
            """,
            [
                ("42", "55", "verified", "moroz_booking_key", NOW),
                ("84", None, "unverified", None, None),
            ],
        )

    async with repository.serialized() as connection:
        resolved = await repository.resolve_identity(
            connection,
            ActivityCandidate("telegram", "84", "unverified", None),
            ("55",),
            now=NOW,
        )

    assert resolved.status == "conflict"
    async with database.acquire() as connection:
        rows = await connection.fetch(
            "SELECT user_id, identity_status FROM customer_activity_projection ORDER BY user_id"
        )
    assert [tuple(row) for row in rows] == [("42", "conflict"), ("84", "conflict")]


async def test_changed_verified_identity_fails_closed(database):
    repository = ActivityRepository(database)
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, yclients_client_id, identity_status,
                 identity_source, identity_verified_at, sync_status)
            VALUES ('telegram', '42', '55', 'verified',
                    'moroz_booking_key', $1, 'current')
            """,
            NOW,
        )

    async with repository.serialized() as connection:
        resolved = await repository.resolve_identity(
            connection,
            ActivityCandidate("telegram", "42", "verified", "55"),
            ("66",),
            now=NOW,
        )

    assert resolved.status == "conflict"
    async with database.acquire() as connection:
        assert await connection.fetchval(
            "SELECT identity_status FROM customer_activity_projection"
        ) == "conflict"


async def test_partial_and_error_never_advance_successful_history_watermark(database):
    repository = ActivityRepository(database)
    watermark = NOW - timedelta(hours=3)
    candidate = ActivityCandidate("telegram", "42", "verified", "55")
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, yclients_client_id, identity_status,
                 history_synced_at, source_version, sync_status)
            VALUES ('telegram', '42', '55', 'verified', $1, 'old', 'current')
            """,
            watermark,
        )

    partial = ClientActivitySnapshot(
        yclients_client_id="55",
        last_completed_visit_at=NOW,
        next_active_booking_at=None,
        history_synced_at=NOW,
        source_version=ACTIVITY_SOURCE_VERSION,
        sync_status="partial",
        error_code="history_page_limit",
    )
    async with repository.serialized() as connection:
        await repository.apply_snapshot(connection, candidate, partial)
        await repository.record_error(
            connection, candidate, "yclients_transport", now=NOW
        )

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT history_synced_at, sync_status, sync_error_code, source_version "
            "FROM customer_activity_projection"
        )
    assert row["history_synced_at"] == watermark
    assert row["sync_status"] == "error"
    assert row["sync_error_code"] == "yclients_transport"
    assert row["source_version"] == "old"


async def test_recent_projection_owns_future_booking_and_preserves_history_columns(database):
    history = NOW - timedelta(hours=1)
    inbound = NOW - timedelta(minutes=5)
    async with database.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO customer_activity_projection
                (channel, user_id, yclients_client_id, identity_status,
                 last_completed_visit_at, last_meaningful_inbound_at,
                 history_synced_at, sync_status)
            VALUES ('telegram', '42', '55', 'verified', $1, $2, $3, 'current')
            """,
            NOW - timedelta(days=100),
            inbound,
            history,
        )
    key = uuid4()
    future = NOW + timedelta(days=2)
    snapshot = ProjectionSnapshot(
        records=(
            _projection_record(
                external_id="9001", booking_key=key, client_id="55", starts_at=future
            ),
        ),
        synced_at=NOW,
    )

    repository = ProjectionRepository(database)
    async with repository.serialized() as connection:
        await repository.replace(connection, snapshot)

    async with database.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT last_completed_visit_at, last_meaningful_inbound_at, "
            "history_synced_at, next_active_booking_at, recent_bookings_synced_at "
            "FROM customer_activity_projection"
        )
        stored = await connection.fetchrow(
            "SELECT client_id, record_created_at FROM yclients_booking_projection"
        )
    assert tuple(row) == (
        NOW - timedelta(days=100),
        inbound,
        history,
        future,
        NOW,
    )
    assert tuple(stored) == ("55", NOW - timedelta(days=100))
