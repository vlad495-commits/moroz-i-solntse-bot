from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from moroz.booking.catalog import CatalogRepository
from moroz.booking.yclients_catalog import (
    CatalogRecord,
    CatalogSnapshot,
    YclientsCatalogError,
)
from moroz.common.db import Database


pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def database(migrated_database_url):
    database = Database(migrated_database_url, min_size=2, max_size=2)
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def snapshot(service_id, *, name="Криотерапия"):
    return CatalogSnapshot(
        (
            CatalogRecord(
                service_id, "10", name, "Крио", "Анна",
                Decimal("1230.00"), Decimal("1500.00"), 3,
            ),
        ),
        NOW,
    )


async def rows(database):
    async with database.acquire() as connection:
        result = await connection.fetch(
            """
            SELECT service_id, staff_id, service_name, category_name,
                   staff_name, price_min, price_max, duration_minutes, synced_at
            FROM yclients_service_catalog
            ORDER BY service_id, staff_id
            """
        )
    return [tuple(row.values()) for row in result]


def expected(value):
    item = value.records[0]
    return [(
        item.service_id, item.staff_id, item.service_name, item.category_name,
        item.staff_name, item.price_min, item.price_max,
        item.duration_minutes, value.synced_at,
    )]


async def test_replace_is_atomic_and_stores_only_allowlisted_columns(database):
    repository = CatalogRepository(database)
    first = snapshot("20")
    second = snapshot("21", name="Новая")

    async with repository.serialized() as connection:
        assert connection is not None
        await repository.replace(connection, first)
    assert await rows(database) == expected(first)

    async with database.acquire() as connection:
        await connection.execute(
            """
            CREATE FUNCTION reject_catalog_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'forced catalog write failure'; END;
            $$;
            CREATE TRIGGER reject_catalog_insert
            BEFORE INSERT ON yclients_service_catalog
            FOR EACH ROW EXECUTE FUNCTION reject_catalog_insert();
            """
        )
    with pytest.raises(YclientsCatalogError, match="^yclients_catalog_write$"):
        async with repository.serialized() as connection:
            assert connection is not None
            await repository.replace(connection, second)

    assert await rows(database) == expected(first)
    async with database.acquire() as connection:
        stored = await connection.fetchval(
            "SELECT to_jsonb(yclients_service_catalog)::text "
            "FROM yclients_service_catalog"
        )
    assert "private-provider-description" not in stored
    assert "phone" not in stored
    assert "payload" not in stored


async def test_serialized_lock_blocks_peer_and_releases_on_body_exception(database):
    first = CatalogRepository(database)
    second = CatalogRepository(database)

    async with first.serialized() as locked:
        assert locked is not None
        async with second.serialized() as blocked:
            assert blocked is None

    with pytest.raises(RuntimeError, match="^sentinel$"):
        async with first.serialized() as locked:
            assert locked is not None
            raise RuntimeError("sentinel")

    async with second.serialized() as released:
        assert released is not None


async def test_list_services_returns_grouped_catalog_choices(database):
    repository = CatalogRepository(database)
    value = snapshot("20", name="Криокапсула")
    async with repository.serialized() as connection:
        assert connection is not None
        await repository.replace(connection, value)
        services = await repository.list_services(connection)

    assert [service.service_name for service in services] == ["Криокапсула"]
    assert services[0].variants[0].staff_name == "Анна"
