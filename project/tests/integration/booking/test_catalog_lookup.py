from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from moroz.booking.catalog import CATALOG_SYNC_KIND, CatalogRepository
from moroz.common.db import Database


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def seed_row(connection, synced_at):
    await connection.execute(
        """
        INSERT INTO yclients_service_catalog
            (service_id, staff_id, service_name, category_name, staff_name,
             price_min, price_max, duration_minutes, synced_at)
        VALUES ('20', '10', 'Криотерапия', 'Крио', 'Анна',
                $1, $2, 3, $3)
        """,
        Decimal("1230.00"),
        Decimal("1500.00"),
        synced_at,
    )


async def seed_job(connection, status, timestamp):
    await connection.execute(
        """
        INSERT INTO scheduler_jobs
            (id, kind, run_at, payload, idempotency_key, status,
             finished_at, created_at, updated_at)
        VALUES ($1, $2::text, $3::timestamptz, '{}'::jsonb, $4::text, $5::text,
                CASE WHEN $5::text = 'finished' THEN $3::timestamptz END,
                $3::timestamptz, $3::timestamptz)
        """,
        uuid4(),
        CATALOG_SYNC_KIND,
        timestamp,
        f"catalog:{status}:{uuid4()}",
        status,
    )


async def test_nonempty_snapshot_is_fresh_at_exact_24_hour_boundary(database):
    async with database.acquire() as connection:
        await seed_row(connection, NOW - timedelta(hours=24))
        result = await CatalogRepository(database).ground(
            connection, "Сколько стоит криотерапия?", NOW
        )

    assert result.status == "fresh"
    assert len(result.services) == 1


async def test_snapshot_one_microsecond_older_is_stale_and_returns_no_rows(database):
    async with database.acquire() as connection:
        await seed_row(connection, NOW - timedelta(hours=24, microseconds=1))
        result = await CatalogRepository(database).ground(
            connection, "Сколько стоит криотерапия?", NOW
        )

    assert result.status == "stale"
    assert result.services == ()
    assert result.simple_kind == "price"


async def test_empty_success_uses_finished_job_and_ignores_newer_non_success(database):
    async with database.acquire() as connection:
        success = NOW - timedelta(hours=23)
        await seed_job(connection, "finished", success)
        for status in ("failed", "skipped", "pending", "claimed"):
            await seed_job(connection, status, NOW - timedelta(hours=1))
        result = await CatalogRepository(database).ground(
            connection, "Сколько стоит криотерапия?", NOW
        )

    assert result.status == "fresh"
    assert result.services == ()


async def test_no_success_is_missing_even_when_failed_job_exists(database):
    async with database.acquire() as connection:
        await seed_job(connection, "failed", NOW - timedelta(minutes=5))
        result = await CatalogRepository(database).ground(
            connection, "Какая цена криотерапии?", NOW
        )

    assert result.status == "missing"
    assert result.services == ()
    assert result.simple_kind == "price"


async def test_ground_requires_aware_now(database):
    async with database.acquire() as connection:
        with pytest.raises(ValueError, match="timezone-aware"):
            await CatalogRepository(database).ground(
                connection, "цена криотерапии", NOW.replace(tzinfo=None)
            )
