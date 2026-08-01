import json

import pytest

from moroz.booking.catalog import CatalogService, CatalogStaff
from moroz.booking.models import BookingTemporaryError
from moroz.booking.yclients_catalog import YclientsCatalogAdapter
from moroz.booking.yclients_http import HttpResponse, YclientsTransportError


class FakeHttp:
    def __init__(self) -> None:
        self.responses: list[HttpResponse | Exception] = []
        self.requests: list[tuple[str, str, bool]] = []

    def queue(self, status: int, body: bytes) -> None:
        self.responses.append(HttpResponse(status, body))

    def queue_json(self, status: int, payload: object) -> None:
        self.queue(status, json.dumps(payload, ensure_ascii=False).encode())

    async def request(
        self, method: str, path: str, *, user_auth: bool = False
    ) -> HttpResponse:
        self.requests.append((method, path, user_auth))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def fake_http() -> FakeHttp:
    return FakeHttp()


@pytest.mark.asyncio
async def test_catalog_returns_only_allowlisted_services_and_staff(
    fake_http: FakeHttp,
) -> None:
    fake_http.queue_json(
        200,
        {
            "success": True,
            "data": [
                {"id": 1, "title": "Крио", "duration": 1800},
                {"id": 9, "title": "Скрытая", "duration": 600},
            ],
        },
    )
    fake_http.queue_json(
        200,
        {
            "success": True,
            "data": [
                {"id": 7, "name": "Анна", "services": [1]},
                {"id": 8, "name": "Скрытый", "services": [1]},
            ],
        },
    )
    adapter = YclientsCatalogAdapter(fake_http, "42", ("1",), ("7",))

    assert await adapter.list_services() == [CatalogService("1", "Крио", 30)]
    assert await adapter.list_staff(("1",)) == [
        CatalogStaff("7", "Анна", ("1",))
    ]
    assert fake_http.requests == [
        ("GET", "/api/v1/book_services/42", False),
        ("GET", "/api/v1/book_staff/42", False),
    ]


@pytest.mark.asyncio
async def test_catalog_staff_must_support_every_selected_service(
    fake_http: FakeHttp,
) -> None:
    fake_http.queue_json(
        200,
        {
            "success": True,
            "data": [
                {"id": 7, "name": "Анна", "services": [1, 2]},
                {"id": 8, "name": "Ирина", "services": [1]},
            ],
        },
    )

    result = await YclientsCatalogAdapter(
        fake_http, "42", ("1", "2"), ("7", "8")
    ).list_staff(("1", "2"))

    assert result == [CatalogStaff("7", "Анна", ("1", "2"))]


@pytest.mark.asyncio
async def test_catalog_staff_does_not_expose_non_allowlisted_service_ids(
    fake_http: FakeHttp,
) -> None:
    fake_http.queue_json(
        200,
        {
            "success": True,
            "data": [
                {"id": 7, "name": "Анна", "services": [1, 9]},
            ],
        },
    )

    result = await YclientsCatalogAdapter(
        fake_http, "42", ("1",), ("7",)
    ).list_staff(("1",))

    assert result == [CatalogStaff("7", "Анна", ("1",))]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [201, 429, 500])
async def test_catalog_http_failure_is_temporary_without_provider_payload(
    fake_http: FakeHttp, status: int
) -> None:
    fake_http.queue(status, b"private provider payload")

    with pytest.raises(BookingTemporaryError) as raised:
        await YclientsCatalogAdapter(
            fake_http, "42", ("1",), ("7",)
        ).list_services()

    assert str(raised.value) == ""


@pytest.mark.asyncio
async def test_catalog_transport_failure_is_temporary_without_transport_detail(
    fake_http: FakeHttp,
) -> None:
    fake_http.responses.append(YclientsTransportError("private transport detail"))

    with pytest.raises(BookingTemporaryError) as raised:
        await YclientsCatalogAdapter(
            fake_http, "42", ("1",), ("7",)
        ).list_services()

    assert str(raised.value) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        b"not-json private body",
        json.dumps({"success": False, "data": []}).encode(),
        json.dumps({"success": True}).encode(),
        json.dumps({"success": True, "data": {}}).encode(),
        json.dumps(
            {
                "success": True,
                "data": [{"id": 1, "title": "Крио", "duration": 1801}],
            }
        ).encode(),
        json.dumps(
            {
                "success": True,
                "data": [{"id": 1, "title": "", "duration": 1800}],
            }
        ).encode(),
    ],
    ids=[
        "invalid-json",
        "unsuccessful-envelope",
        "missing-data",
        "non-list-data",
        "fractional-minute-duration",
        "empty-title",
    ],
)
async def test_service_catalog_malformed_response_fails_closed(
    fake_http: FakeHttp, payload: bytes
) -> None:
    fake_http.queue(200, payload)

    with pytest.raises(BookingTemporaryError) as raised:
        await YclientsCatalogAdapter(
            fake_http, "42", ("1",), ("7",)
        ).list_services()

    assert str(raised.value) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        {"id": 7, "name": "", "services": [1]},
        {"id": 7, "name": "Анна", "services": "1"},
        {"id": 7, "name": "Анна", "services": [True]},
        {"id": 7, "name": "Анна", "services": []},
    ],
    ids=["empty-name", "non-list-services", "boolean-service", "empty-services"],
)
async def test_staff_catalog_malformed_response_fails_closed(
    fake_http: FakeHttp, item: dict[str, object]
) -> None:
    fake_http.queue_json(200, {"success": True, "data": [item]})

    with pytest.raises(BookingTemporaryError) as raised:
        await YclientsCatalogAdapter(
            fake_http, "42", ("1",), ("7",)
        ).list_staff(("1",))

    assert str(raised.value) == ""
