from collections import deque
from datetime import UTC, datetime
from uuid import UUID

import pytest

from moroz.booking.yclients_http import HttpResponse, YclientsConfig, YclientsTransportError
from moroz.booking.yclients_records import YclientsProjectionError, YclientsRecordsReader


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
BOOKING_KEY = UUID("3b53e155-7fd7-4dd0-9ff3-871e0db59577")


class FakeHttp:
    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self._responses = deque(responses)
        self.requests: list[tuple[str, str, tuple[tuple[str, object], ...], bool]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: tuple[tuple[str, object], ...] = (),
        user_auth: bool = False,
    ) -> HttpResponse:
        self.requests.append((method, path, tuple(query), user_auth))
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def _config() -> YclientsConfig:
    return YclientsConfig(
        base_url="https://example.invalid",
        partner_token="partner",
        user_token="user",
        company_id=17,
    )


def _record(record_id: int = 9001, **changes: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": record_id,
        "client": {"name": "  Client  "},
        "staff": {"name": "  Specialist  "},
        "services": [{"title": "  Cryotherapy  "}],
        "datetime": "2026-08-14T12:00:00+03:00",
        "seance_length": 1800,
        "attendance": 0,
        "deleted": False,
        "custom_fields": {"moroz_booking_key": str(BOOKING_KEY)},
    }
    record.update(changes)
    return record


def _response(records: object, *, status: int = 200) -> HttpResponse:
    import json

    return HttpResponse(status, json.dumps({"success": True, "data": records}).encode())


def _page(records: list[dict[str, object]]) -> HttpResponse:
    return _response(records)


@pytest.mark.asyncio
async def test_reads_exact_window_pages_and_projects_allowlisted_fields() -> None:
    first = [_record(index) for index in range(1, 101)]
    fake = FakeHttp([_page(first), _page([_record(101)])])

    snapshot = await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert fake.requests == [
        ("GET", "/api/v1/records/17", (
            ("page", 1), ("count", 100),
            ("start_date", "2026-07-15"),
            ("end_date", "2026-11-12"),
            ("with_deleted", 1),
        ), True),
        ("GET", "/api/v1/records/17", (
            ("page", 2), ("count", 100),
            ("start_date", "2026-07-15"),
            ("end_date", "2026-11-12"),
            ("with_deleted", 1),
        ), True),
    ]
    record = snapshot.records[0]
    assert record.external_id == "1"
    assert record.booking_key == BOOKING_KEY
    assert record.bot_marker_state == "valid"
    assert record.starts_at.tzinfo is not None
    assert record.scheduled_end_at == record.starts_at.replace(minute=30)
    assert record.status == "confirmed"
    assert (record.client_name, record.staff_name, record.service_names) == (
        "Client", "Specialist", ("Cryotherapy",),
    )
    assert not hasattr(record, "phone")
    assert not hasattr(record, "email")
    assert not hasattr(record, "comment")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker", "state", "expected_key"),
    [
        (None, "absent", None),
        ("not-a-uuid", "invalid", None),
        (str(BOOKING_KEY), "valid", BOOKING_KEY),
    ],
)
async def test_projects_booking_marker_state(
    marker: str | None, state: str, expected_key: UUID | None,
) -> None:
    fields = {} if marker is None else {"moroz_booking_key": marker}
    fake = FakeHttp([_page([_record(custom_fields=fields)])])

    snapshot = await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert (snapshot.records[0].bot_marker_state, snapshot.records[0].booking_key) == (
        state, expected_key,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attendance", "deleted", "expected"),
    [
        (-1, False, "no_show"),
        (1, False, "completed"),
        (99, False, "unknown"),
        (0, True, "cancelled"),
    ],
)
async def test_projects_normalized_visit_status(
    attendance: int, deleted: bool, expected: str,
) -> None:
    fake = FakeHttp([_page([_record(attendance=attendance, deleted=deleted)])])

    snapshot = await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert snapshot.records[0].status == expected


@pytest.mark.asyncio
async def test_removes_display_controls_and_bounds_display_text() -> None:
    unsafe = " \x00" + "x" * 250 + "\t\n "
    fake = FakeHttp([_page([_record(
        client={"name": unsafe},
        staff={"name": " \x00 Ada\n "},
        services=[{"title": " \x00 Ice\t "}],
    )])])

    snapshot = await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    record = snapshot.records[0]
    assert record.client_name == "x" * 200
    assert record.staff_name == "Ada"
    assert record.service_names == ("Ice",)


@pytest.mark.asyncio
async def test_rejects_duplicate_provider_id() -> None:
    first = [_record(index) for index in range(1, 101)]
    fake = FakeHttp([_page(first), _page([_record(1)])])

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert raised.value.code == "yclients_response_shape"


@pytest.mark.asyncio
async def test_rejects_duplicate_provider_id_within_one_page() -> None:
    records = [_record(1), _record(1)] + [_record(index) for index in range(3, 101)]
    fake = FakeHttp([_page(records), _page([])])

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert raised.value.code == "yclients_response_shape"


@pytest.mark.asyncio
async def test_rejects_a_full_hundredth_page() -> None:
    pages = [
        _page([_record(page * 100 + index) for index in range(1, 101)])
        for page in range(100)
    ]
    fake = FakeHttp(pages)

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert raised.value.code == "yclients_page_bound"
    assert len(fake.requests) == 100


@pytest.mark.asyncio
async def test_rejects_provider_page_larger_than_hard_bound() -> None:
    fake = FakeHttp([_page([_record(index) for index in range(1, 102)]), _page([])])

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert raised.value.code == "yclients_response_shape"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        HttpResponse(500, b"{}"),
        HttpResponse(200, b"not-json"),
        _response({"not": "a list"}),
        _page([{"id": 1}]),
        _page([_record(services=[{"title": "service"}] * 51)]),
    ],
)
async def test_rejects_http_and_malformed_provider_data(response: HttpResponse) -> None:
    fake = FakeHttp([response])

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert raised.value.code in {
        "yclients_http_status",
        "yclients_response_shape",
    }
    assert str(raised.value) == raised.value.code


@pytest.mark.asyncio
async def test_rejects_transport_failure_with_safe_code() -> None:
    fake = FakeHttp([YclientsTransportError()])

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert raised.value.code == "yclients_transport"
    assert str(raised.value) == "yclients_transport"
