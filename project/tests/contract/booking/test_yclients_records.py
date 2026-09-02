from collections import deque
from datetime import UTC, datetime
from uuid import UUID

import pytest

import moroz.booking.yclients_records as yclients_records
from moroz.booking.yclients_http import HttpResponse, YclientsConfig, YclientsTransportError
from moroz.booking.yclients_records import YclientsProjectionError, YclientsRecordsReader
from moroz.reactivation.activity import (
    MAX_HISTORY_PAGES,
    YclientsClientHistoryReader,
)


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
        "create_date": "2026-01-02T09:00:00+03:00",
        "client": {
            "id": 55,
            "name": "  Client  ",
            "phone": "+79990000000",
        },
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
    assert record.client_id == "55"
    assert record.record_created_at == datetime(2026, 1, 2, 6, 0, tzinfo=UTC)
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
@pytest.mark.parametrize("client", [None, {}, {"id": None}])
async def test_empty_client_has_no_stable_identity(client: object) -> None:
    snapshot = await YclientsRecordsReader(
        _config(), http=FakeHttp([_page([_record(client=client)])])
    ).read_window(NOW)

    assert snapshot.records[0].client_id is None


@pytest.mark.asyncio
async def test_missing_or_null_create_date_is_not_identity_proof() -> None:
    missing = _record()
    missing.pop("create_date")

    for record in (missing, _record(create_date=None)):
        snapshot = await YclientsRecordsReader(
            _config(), http=FakeHttp([_page([record])])
        ).read_window(NOW)

        assert snapshot.records[0].record_created_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("create_date", [False, 0, "not-a-date", {}])
async def test_malformed_non_null_create_date_fails_provider_page_safely(
    create_date: object,
) -> None:
    reader = YclientsRecordsReader(
        _config(), http=FakeHttp([_page([_record(create_date=create_date)])])
    )

    with pytest.raises(YclientsProjectionError) as raised:
        await reader.read_window(NOW)

    assert raised.value.code == "yclients_response_shape"


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
async def test_accepts_service_less_records_when_services_are_missing_or_null() -> None:
    missing = _record()
    missing.pop("services")

    for record in (missing, _record(services=None)):
        snapshot = await YclientsRecordsReader(
            _config(), http=FakeHttp([_page([record])])
        ).read_window(NOW)

        assert snapshot.records[0].service_names == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("services", [False, 0, "service", {}])
async def test_rejects_non_list_services(services: object) -> None:
    fake = FakeHttp([_page([_record(services=services)])])

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert raised.value.code == "yclients_response_shape"


@pytest.mark.asyncio
async def test_rejects_provider_id_longer_than_cursor_contract_before_projection(
    monkeypatch,
) -> None:
    def forbidden_projection_record(**_values):
        raise AssertionError("invalid provider id reached ProjectionRecord")

    monkeypatch.setattr(yclients_records, "ProjectionRecord", forbidden_projection_record)
    fake = FakeHttp([_page([_record(int("1" * 65))])])

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsRecordsReader(_config(), http=fake).read_window(NOW)

    assert raised.value.code == "yclients_response_shape"


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


@pytest.mark.asyncio
async def test_full_history_is_paginated_by_stable_client_id_and_aggregated() -> None:
    completed = _record(
        1,
        attendance=1,
        datetime="2026-08-01T12:00:00+03:00",
        client={"id": 55, "phone": "+79990000000", "name": "Test"},
    )
    cancelled = _record(
        2,
        attendance=1,
        deleted=True,
        datetime="2026-08-10T12:00:00+03:00",
    )
    future = _record(
        3,
        attendance=0,
        datetime="2026-08-20T12:00:00+03:00",
    )
    fake = FakeHttp([_page([completed, cancelled, future])])

    snapshot = await YclientsClientHistoryReader(
        _config(), http=fake
    ).read_history("55", now=NOW)

    assert fake.requests == [
        (
            "GET",
            "/api/v1/records/17",
            (("client_id", "55"), ("page", 1), ("count", 100), ("with_deleted", 1)),
            True,
        )
    ]
    assert snapshot.yclients_client_id == "55"
    assert snapshot.last_completed_visit_at == datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    assert snapshot.next_active_booking_at == datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    assert snapshot.history_synced_at == NOW
    assert snapshot.sync_status == "current"
    assert snapshot.error_code is None
    assert "+79990000000" not in repr(snapshot)


@pytest.mark.asyncio
async def test_history_page_limit_is_bounded_and_partial() -> None:
    pages = [
        _page([_record(page * 100 + index) for index in range(1, 101)])
        for page in range(MAX_HISTORY_PAGES)
    ]
    fake = FakeHttp(pages)

    snapshot = await YclientsClientHistoryReader(
        _config(), http=fake
    ).read_history("55", now=NOW)

    assert len(fake.requests) == MAX_HISTORY_PAGES
    assert all(dict(request[2])["client_id"] == "55" for request in fake.requests)
    assert snapshot.sync_status == "partial"
    assert snapshot.error_code == "history_page_limit"


@pytest.mark.asyncio
async def test_history_rejects_malformed_non_null_record_create_date() -> None:
    fake = FakeHttp([_page([_record(create_date="not-a-date")])])

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsClientHistoryReader(_config(), http=fake).read_history(
            "55", now=NOW
        )

    assert raised.value.code == "yclients_response_shape"


@pytest.mark.asyncio
@pytest.mark.parametrize("client", [None, {"id": 66, "name": "Other"}])
async def test_history_rejects_record_not_owned_by_requested_client(
    client: object,
) -> None:
    fake = FakeHttp([_page([_record(client=client)])])

    with pytest.raises(YclientsProjectionError) as raised:
        await YclientsClientHistoryReader(_config(), http=fake).read_history(
            "55", now=NOW
        )

    assert raised.value.code == "yclients_response_shape"


@pytest.mark.asyncio
async def test_single_record_lookup_uses_provider_id_and_safe_projection() -> None:
    fake = FakeHttp([_response(_record(9001))])

    record = await YclientsClientHistoryReader(
        _config(), http=fake
    ).read_record("9001")

    assert fake.requests == [
        ("GET", "/api/v1/record/17/9001", (), True)
    ]
    assert record is not None
    assert record.external_id == "9001"
    assert record.client_id == "55"
    assert not hasattr(record, "phone")


@pytest.mark.asyncio
async def test_single_record_lookup_accepts_provider_single_item_list_envelope() -> None:
    fake = FakeHttp([_response([_record(9001)])])

    record = await YclientsClientHistoryReader(
        _config(), http=fake
    ).read_record("9001")

    assert record is not None
    assert record.client_id == "55"
