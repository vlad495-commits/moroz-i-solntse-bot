import json

import pytest

from moroz.booking.catalog import CatalogService, CatalogStaff
from moroz.booking.models import BookingTemporaryError
from moroz.booking.yclients_catalog import YclientsCatalogAdapter
from moroz.booking.yclients_http import HttpResponse, YclientsTransportError


class FakeHttp:
    def __init__(self) -> None:
        self.responses: list[HttpResponse | Exception] = []
        self.requests: list[
            tuple[str, str, tuple[tuple[str, object], ...], bool]
        ] = []

    def queue(self, status: int, body: bytes) -> None:
        self.responses.append(HttpResponse(status, body))

    def queue_json(self, status: int, payload: object) -> None:
        self.queue(status, json.dumps(payload, ensure_ascii=False).encode())

    async def request(
        self,
        method: str,
        path: str,
        *,
        query=(),
        json_body=None,
        user_auth: bool = False,
    ) -> HttpResponse:
        assert json_body is None
        self.requests.append((method, path, tuple(query), user_auth))
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
            "data": {
                "services": [
                    {"id": 1, "title": "Крио", "seance_length": 1800},
                    {"id": 9, "title": "Скрытая", "seance_length": 600},
                ],
            },
        },
    )
    fake_http.queue_json(
        200,
        {
            "success": True,
            "data": [
                {"id": 7, "name": "Анна", "bookable": True},
                {"id": 8, "name": "Скрытый", "bookable": True},
            ],
        },
    )
    adapter = YclientsCatalogAdapter(fake_http, "42", ("1",), ("7",))

    assert await adapter.list_services() == [CatalogService("1", "Крио", 30)]
    assert await adapter.list_staff(("1",)) == [
        CatalogStaff("7", "Анна", ("1",))
    ]
    assert fake_http.requests == [
        ("GET", "/api/v1/book_services/42", (), False),
        (
            "GET",
            "/api/v1/book_staff/42",
            (("service_ids[]", "1"),),
            False,
        ),
    ]


@pytest.mark.asyncio
async def test_catalog_parses_service_seance_length_from_live_contract(
    fake_http: FakeHttp,
) -> None:
    fake_http.queue_json(
        200,
        {
            "success": True,
            "data": {
                "services": [
                    {"id": 1, "title": "Крио", "seance_length": 1800},
                ],
            },
        },
    )

    result = await YclientsCatalogAdapter(
        fake_http, "42", ("1",), ("7",)
    ).list_services()

    assert result == [CatalogService("1", "Крио", 30)]


@pytest.mark.asyncio
async def test_catalog_accepts_live_null_service_seance_length(
    fake_http: FakeHttp,
) -> None:
    fake_http.queue_json(
        200,
        {
            "success": True,
            "data": {
                "services": [
                    {"id": 1, "title": "Крио", "seance_length": None},
                ],
            },
        },
    )

    result = await YclientsCatalogAdapter(
        fake_http, "42", ("1",), ("7",)
    ).list_services()

    assert result == [CatalogService("1", "Крио", None)]


@pytest.mark.asyncio
async def test_catalog_staff_uses_selected_services_and_excludes_unbookable(
    fake_http: FakeHttp,
) -> None:
    fake_http.queue_json(
        200,
        {
            "success": True,
            "data": [
                {"id": 7, "name": "Анна", "bookable": True},
                {"id": 8, "name": "Ирина", "bookable": False},
                {"id": 9, "name": "Скрытый", "bookable": True},
            ],
        },
    )

    result = await YclientsCatalogAdapter(
        fake_http, "42", ("1", "2"), ("7", "8")
    ).list_staff(("1", "2"))

    assert result == [CatalogStaff("7", "Анна", ("1", "2"))]
    assert fake_http.requests == [
        (
            "GET",
            "/api/v1/book_staff/42",
            (("service_ids[]", "1"), ("service_ids[]", "2")),
            False,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service_ids",
    [(), ("0",), ("abc",), ("9",), ("1", "1")],
)
async def test_catalog_staff_rejects_invalid_or_disallowed_services_before_http(
    fake_http: FakeHttp, service_ids: tuple[str, ...]
) -> None:
    with pytest.raises(BookingTemporaryError):
        await YclientsCatalogAdapter(
            fake_http, "42", ("1",), ("7",)
        ).list_staff(service_ids)

    assert fake_http.requests == []


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
        json.dumps({"success": False, "data": {"services": []}}).encode(),
        json.dumps({"success": True}).encode(),
        json.dumps({"success": True, "data": {}}).encode(),
        json.dumps({"success": True, "data": {"services": {}}}).encode(),
        json.dumps({"success": True, "data": []}).encode(),
        json.dumps(
            {
                "success": True,
                "data": {
                    "services": [
                        {"id": 1, "title": "Крио", "seance_length": 1801}
                    ]
                },
            }
        ).encode(),
        json.dumps(
            {
                "success": True,
                "data": {
                    "services": [
                        {"id": 1, "title": "Крио", "seance_length": 1800.0}
                    ]
                },
            }
        ).encode(),
        json.dumps(
            {
                "success": True,
                "data": {
                    "services": [
                        {"id": 1, "title": "Крио", "seance_length": "1800"}
                    ]
                },
            }
        ).encode(),
        json.dumps(
            {
                "success": True,
                "data": {
                    "services": [
                        {"id": 1, "title": "Крио", "seance_length": True}
                    ]
                },
            }
        ).encode(),
        json.dumps(
            {
                "success": True,
                "data": {
                    "services": [
                        {"id": 1, "title": "", "seance_length": 1800}
                    ]
                },
            }
        ).encode(),
    ],
    ids=[
        "invalid-json",
        "unsuccessful-envelope",
        "missing-data",
        "missing-services",
        "non-list-services",
        "non-object-data",
        "fractional-minute-duration",
        "float-duration",
        "string-duration",
        "boolean-duration",
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
        {"id": 7, "name": "", "bookable": True},
        {"id": 7, "name": "Анна"},
        {"id": 7, "name": "Анна", "bookable": "true"},
    ],
    ids=["empty-name", "missing-bookable", "non-boolean-bookable"],
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
