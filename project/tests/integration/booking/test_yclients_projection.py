from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.booking.yclients_records import ProjectionRecord, ProjectionSnapshot
from moroz.booking.projection import ProjectionRepository
from moroz.booking.yclients_records import YclientsProjectionError
from moroz.common.db import Database


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
PHONE = "+79990001122"
COMMENT = "source-only-private-comment"
RAW_JSON = '{"source_only": true}'


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=2, max_size=2)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def snapshot(*, suffix: str, offset: int = 0) -> ProjectionSnapshot:
    starts_at = NOW + timedelta(hours=offset)
    return ProjectionSnapshot(
        records=(
            ProjectionRecord(
                external_id=f"record-{suffix}",
                booking_key=uuid4(),
                bot_marker_state="valid",
                starts_at=starts_at,
                scheduled_end_at=starts_at + timedelta(minutes=45),
                status="confirmed",
                deleted=False,
                client_name="Иван",
                staff_name="Мария",
                service_names=("Солярий", "Криотерапия"),
            ),
        ),
        synced_at=NOW,
    )


async def projection_rows(database):
    async with database.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT external_id, booking_key, bot_marker_state, starts_at,
                   scheduled_end_at, status, deleted, client_name, staff_name,
                   service_names, synced_at
            FROM yclients_booking_projection
            ORDER BY external_id
            """
        )
    return [tuple(row.values()) for row in rows]


async def install_rejecting_trigger(database):
    async with database.acquire() as connection:
        await connection.execute(
            """
            CREATE FUNCTION reject_yclients_projection_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'forced projection write failure';
            END;
            $$;
            CREATE TRIGGER reject_yclients_projection_insert
            BEFORE INSERT ON yclients_booking_projection
            FOR EACH ROW EXECUTE FUNCTION reject_yclients_projection_insert();
            """
        )


async def test_replace_is_atomic_and_persists_only_allowlisted_projection(database):
    repository = ProjectionRepository(database)
    first = snapshot(suffix="first")
    second = snapshot(suffix="second", offset=1)

    async with repository.serialized() as connection:
        assert connection is not None
        await repository.replace(connection, first)
    expected_first = await projection_rows(database)

    await install_rejecting_trigger(database)
    with pytest.raises(YclientsProjectionError, match="^yclients_projection_write$"):
        async with repository.serialized() as connection:
            assert connection is not None
            await repository.replace(connection, second)

    assert await projection_rows(database) == expected_first
    async with database.acquire() as connection:
        stored = await connection.fetchval(
            "SELECT to_jsonb(yclients_booking_projection)::text "
            "FROM yclients_booking_projection"
        )
    assert PHONE not in stored
    assert COMMENT not in stored
    assert RAW_JSON not in stored


async def test_serialized_uses_a_session_advisory_lock(database):
    first = ProjectionRepository(database)
    second = ProjectionRepository(database)

    async with first.serialized() as locked_connection:
        assert locked_connection is not None
        async with second.serialized() as blocked_connection:
            assert blocked_connection is None

    async with second.serialized() as released_connection:
        assert released_connection is not None
