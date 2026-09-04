import json
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from moroz.booking.yclients_catalog import (
    YclientsCatalogError,
    YclientsCatalogReader,
)
from moroz.booking.yclients_http import (
    HttpResponse,
    YclientsConfig,
    YclientsTransportError,
)


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


class FakeHttp:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.requests = []

    async def request(self, method, path, *, query=(), json_body=None, user_auth=False):
        self.requests.append((method, path, tuple(query), user_auth))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def config():
    return YclientsConfig(
        base_url="https://example.invalid",
        partner_token="partner",
        user_token="user",
        company_id=17,
    )


def response(data, *, status=200, success=True):
    return HttpResponse(
        status,
        json.dumps({"success": success, "data": data}).encode("utf-8"),
    )


def staff(staff_id=10, **changes):
    value = {"id": staff_id, "name": " \x00Анна\n ", "bookable": True}
    value.update(changes)
    return value


def service(service_id=20, **changes):
    value = {
        "id": service_id,
        "title": " Криотерапия ",
        "category": {"title": " Крио "},
        "price_min": "1230.00",
        "price_max": 1500,
        "seance_length": 180,
    }
    value.update(changes)
    return value


@pytest.mark.asyncio
async def test_reads_bookable_staff_services_and_normalizes_allowlisted_fields():
    fake = FakeHttp([
        response([staff(), staff(11, name="Скрытый", bookable=False)]),
        response({"services": [service()]}),
    ])

    snapshot = await YclientsCatalogReader(config(), http=fake).read(NOW)

    assert fake.requests == [
        ("GET", "/api/v1/book_staff/17", (("without_seances", 1),), False),
        ("GET", "/api/v1/book_services/17", (("staff_id", 10),), False),
    ]
    assert snapshot.synced_at == NOW
    assert len(snapshot.records) == 1
    record = snapshot.records[0]
    assert (
        record.service_id,
        record.staff_id,
        record.service_name,
        record.category_name,
        record.staff_name,
        record.price_min,
        record.price_max,
        record.duration_minutes,
    ) == (
        "20",
        "10",
        "Криотерапия",
        "Крио",
        "Анна",
        Decimal("1230.00"),
        Decimal("1500.00"),
        3,
    )
    assert not hasattr(record, "description")
    assert not hasattr(record, "raw")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title,seance_length,expected_minutes",
    [
        ("Солярий | 1 минута", 0, 1),
        ("Коллариум 3 минуты", 300, 3),
        ("КОЛЛАГЕНАРИЙ 25 минут", 1800, 25),
    ],
)
async def test_uses_title_minutes_for_confirmed_walk_in_services(
    title, seance_length, expected_minutes
):
    fake = FakeHttp([
        response([staff()]),
        response({
            "services": [
                service(title=title, seance_length=seance_length)
            ]
        }),
    ])

    snapshot = await YclientsCatalogReader(config(), http=fake).read(NOW)

    assert snapshot.records[0].duration_minutes == expected_minutes


@pytest.mark.asyncio
async def test_accepts_empty_successful_snapshot():
    snapshot = await YclientsCatalogReader(
        config(), http=FakeHttp([response([])])
    ).read(NOW)

    assert snapshot.records == ()
    assert snapshot.synced_at == NOW


@pytest.mark.asyncio
async def test_sorts_records_and_rejects_duplicate_service_staff_pair():
    first = service(21, title="Я", price_min=1, price_max=1, seance_length=60)
    second = service(20, title="А", price_min=2, price_max=2, seance_length=120)
    fake = FakeHttp([response([staff()]), response({"services": [first, second]})])
    snapshot = await YclientsCatalogReader(config(), http=fake).read(NOW)
    assert [(item.service_id, item.staff_id) for item in snapshot.records] == [
        ("20", "10"),
        ("21", "10"),
    ]

    duplicate = FakeHttp([
        response([staff()]),
        response({"services": [service(), service()]}),
    ])
    with pytest.raises(YclientsCatalogError, match="^yclients_catalog_response_shape$"):
        await YclientsCatalogReader(config(), http=duplicate).read(NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"id": True},
        {"id": "01"},
        {"id": "9" * 65},
        {"title": None},
        {"price_min": "NaN"},
        {"price_min": -1},
        {"price_max": "100000000.00"},
        {"price_min": 10, "price_max": 9},
        {"seance_length": 61},
        {"seance_length": 0},
        {"seance_length": 86460},
        {"category": "not-an-object"},
    ],
)
async def test_rejects_malformed_service_values(changes):
    fake = FakeHttp([
        response([staff()]),
        response({"services": [service(**changes)]}),
    ])

    with pytest.raises(YclientsCatalogError) as raised:
        await YclientsCatalogReader(config(), http=fake).read(NOW)

    assert raised.value.code == "yclients_catalog_response_shape"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses,code",
    [
        ([YclientsTransportError()], "yclients_catalog_transport"),
        ([response([], status=503)], "yclients_catalog_http_status"),
        ([HttpResponse(200, b"not-json")], "yclients_catalog_response_shape"),
        ([response([], success=False)], "yclients_catalog_response_shape"),
        ([response({})], "yclients_catalog_response_shape"),
        ([response([staff(index) for index in range(1, 102)])], "yclients_catalog_bound"),
    ],
)
async def test_maps_transport_http_shape_and_staff_bound_to_safe_codes(responses, code):
    with pytest.raises(YclientsCatalogError) as raised:
        await YclientsCatalogReader(config(), http=FakeHttp(responses)).read(NOW)
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_rejects_service_and_total_pair_bounds():
    too_many = [service(index) for index in range(1, 202)]
    fake = FakeHttp([response([staff()]), response({"services": too_many})])
    with pytest.raises(YclientsCatalogError, match="^yclients_catalog_bound$"):
        await YclientsCatalogReader(config(), http=fake).read(NOW)


@pytest.mark.asyncio
async def test_rejects_inconsistent_service_identity_between_staff():
    fake = FakeHttp([
        response([staff(10), staff(11, name="Мария")]),
        response({"services": [service(20, title="Криотерапия")]}),
        response({"services": [service(20, title="Другое название")]}),
    ])

    with pytest.raises(YclientsCatalogError, match="^yclients_catalog_response_shape$"):
        await YclientsCatalogReader(config(), http=fake).read(NOW)
