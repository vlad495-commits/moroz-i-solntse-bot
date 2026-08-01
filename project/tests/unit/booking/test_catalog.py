import pytest

from moroz.booking.catalog import (
    CatalogService,
    CatalogStaff,
    parse_id_allowlist,
)
from moroz.booking.mock_catalog import MockBookingCatalog


def test_allowlist_is_numeric_unique_and_non_empty():
    assert parse_id_allowlist("17, 29", "services") == ("17", "29")
    for invalid in ("", "17,17", "17,nope"):
        with pytest.raises(ValueError):
            parse_id_allowlist(invalid, "services")


@pytest.mark.asyncio
async def test_mock_catalog_returns_configured_services():
    services = (
        CatalogService("1", "Крио", 30),
        CatalogService("2", "Массаж", 60),
    )

    catalog = MockBookingCatalog(
        services=services,
        staff=(),
        service_allowlist=("1", "2"),
        staff_allowlist=(),
    )

    assert await catalog.list_services() == [*services]


@pytest.mark.asyncio
async def test_mock_catalog_filters_staff_for_all_selected_services():
    catalog = MockBookingCatalog(
        services=(
            CatalogService("1", "Крио", 30),
            CatalogService("2", "Массаж", 60),
        ),
        staff=(
            CatalogStaff("7", "Анна", ("1", "2")),
            CatalogStaff("8", "Ирина", ("1",)),
        ),
        service_allowlist=("1", "2"),
        staff_allowlist=("7", "8"),
    )

    assert await catalog.list_staff(("1", "2")) == [
        CatalogStaff("7", "Анна", ("1", "2"))
    ]


@pytest.mark.asyncio
async def test_mock_catalog_hides_fixtures_outside_allowlists():
    catalog = MockBookingCatalog(
        services=(
            CatalogService("1", "Крио", 30),
            CatalogService("2", "Скрытая услуга", 60),
        ),
        staff=(
            CatalogStaff("7", "Анна", ("1", "2")),
            CatalogStaff("8", "Скрытый мастер", ("1",)),
        ),
        service_allowlist=("1",),
        staff_allowlist=("7",),
    )

    assert await catalog.list_services() == [CatalogService("1", "Крио", 30)]
    assert await catalog.list_staff(("1",)) == [
        CatalogStaff("7", "Анна", ("1",))
    ]
