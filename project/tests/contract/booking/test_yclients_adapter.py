import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import pytest

from moroz.booking.models import (
    BookingNotFound,
    BookingOutcomeUnknown,
    BookingTemporaryError,
    CancelBooking,
    CreateBooking,
    RescheduleBooking,
    SlotQuery,
    SlotUnavailable,
)
from moroz.booking.yclients import YclientsAdapter
from moroz.booking.yclients_http import YclientsConfig, YclientsHttpClient


class ScriptedServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.responses: list[tuple[int | None, object]] = []
        self.requests: list[tuple[str, str, dict[str, str], object | None]] = []
        super().__init__(("127.0.0.1", 0), ScriptedHandler)


class ScriptedHandler(BaseHTTPRequestHandler):
    server: ScriptedServer

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def do_PUT(self) -> None:
        self._respond()

    def do_DELETE(self) -> None:
        self._respond()

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.server.requests.append((
            self.command,
            self.path,
            dict(self.headers),
            json.loads(raw) if raw else None,
        ))
        status, payload = self.server.responses.pop(0)
        if status is None:
            self.close_connection = True
            return
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[ScriptedServer]:
    instance = ScriptedServer()
    thread = Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        thread.join()
        instance.server_close()


def _config(server: ScriptedServer) -> YclientsConfig:
    host, port = server.server_address
    return YclientsConfig(
        base_url=f"http://{host}:{port}",
        partner_token="partner-value",
        user_token="user-value",
        company_id=123,
    )


def _slot_id(
    config: YclientsConfig,
    *,
    services: list[int] | None = None,
    staff: int = 6544,
    start: int = 1785315600,
    duration: int = 3600,
) -> str:
    raw = json.dumps(
        {"duration": duration, "services": services or [331], "staff": staff, "start": start},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    message = b"yclients-slot:v1\0" + str(config.company_id).encode() + b"\0" + raw
    tag = hmac.new(config.user_token.encode(), message, hashlib.sha256).digest()[:16]
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return f"yclients:v1:{encode(raw)}.{encode(tag)}"


def _record(
    *, api_id: str = "moroz:v1:Y3VzdG9tZXItNw", deleted: bool = False, **changes: object,
) -> dict[str, object]:
    record = {
        "id": 9001,
        "staff_id": "6544",
        "services": [{"id": "331"}],
        "datetime": "2026-07-29T12:00:00+03:00",
        "seance_length": 3600,
        "api_id": api_id,
        "deleted": deleted,
    }
    record.update(changes)
    return record


def _availability(server: ScriptedServer) -> None:
    server.responses.extend([
        (200, {"success": True, "data": {"booking_dates": ["2026-07-29"]}}),
        (200, {"success": True, "data": [{"id": "6544", "bookable": True}]}),
        (200, {"success": True, "data": [
            {"datetime": 1785315600, "seance_length": 3600},
            {"datetime": "1785315600", "seance_length": "3600"},
        ]}),
    ])


@pytest.mark.asyncio
async def test_availability_create_and_get_use_official_contract_without_cache(
    server: ScriptedServer,
) -> None:
    _availability(server)
    config = _config(server)
    timezone = ZoneInfo("Europe/Moscow")
    slots = await YclientsAdapter(config).list_slots(SlotQuery(
        service_ids=("331",),
        starts_after=datetime(2026, 7, 29, 11, 59, tzinfo=timezone),
        starts_before=datetime(2026, 7, 29, 13, 0, tzinfo=timezone),
    ))

    assert [(slot.staff_id, slot.duration_minutes) for slot in slots] == [("6544", 60)]
    assert slots[0].id.startswith("yclients:v1:")

    server.responses.extend([
        (201, {"success": True, "data": {}}),
        (201, {"success": True, "data": _record()}),
        (200, {"success": True, "data": _record()}),
    ])
    booking = await YclientsAdapter(config).create_booking(CreateBooking(
        customer_id="customer-7",
        slot_id=slots[0].id,
        idempotency_key="local-key-only",
        customer_name="Sandbox Customer",
        customer_phone="+70000000000",
        personal_data_processing_allowed=True,
        comment="contract test",
    ))
    fetched = await YclientsAdapter(config).get_booking(booking.external_id)

    assert booking.customer_id == "customer-7"
    assert booking.slot_id == slots[0].id
    assert fetched == booking
    paths = [urlsplit(request[1]).path for request in server.requests]
    assert paths == [
        "/api/v1/book_dates/123",
        "/api/v1/book_staff/123",
        "/api/v1/book_times/123/6544/2026-07-29",
        "/api/v1/book_check/123",
        "/api/v1/records/123",
        "/api/v1/record/123/9001",
    ]
    assert parse_qsl(urlsplit(server.requests[0][1]).query) == [
        ("service_ids", "331"),
        ("date_from", "2026-07-29"),
        ("date_to", "2026-07-29"),
    ]
    assert parse_qsl(urlsplit(server.requests[1][1]).query) == [("service_ids", "331")]
    assert parse_qsl(urlsplit(server.requests[2][1]).query) == [("service_ids", "331")]
    assert server.requests[3][2]["Authorization"] == "Bearer partner-value"
    assert server.requests[4][2]["Authorization"] == "Bearer partner-value, User user-value"
    assert server.requests[5][2]["Authorization"] == "Bearer partner-value, User user-value"
    assert server.requests[3][3] == {"appointments": [{
        "id": 1,
        "services": [331],
        "staff_id": 6544,
        "datetime": 1785315600,
    }]}
    assert server.requests[4][3] == {
        "staff_id": 6544,
        "services": [{"id": 331}],
        "client": {"name": "Sandbox Customer", "phone": "+70000000000"},
        "save_if_busy": False,
        "datetime": "2026-07-29 12:00:00",
        "seance_length": 3600,
        "send_sms": False,
        "comment": "contract test",
        "attendance": 0,
        "api_id": "moroz:v1:Y3VzdG9tZXItNw",
        "client_agreements": {
            "is_personal_data_processing_allowed": True,
            "is_newsletter_allowed": False,
        },
    }
    assert all("Idempotency-Key" not in request[2] for request in server.requests)
    assert "local-key-only" not in json.dumps(server.requests)


@pytest.mark.asyncio
async def test_availability_accepts_epoch_dates_iso_times_ids_and_filters_exact_bounds(
    server: ScriptedServer,
) -> None:
    server.responses.extend([
        (200, {"success": True, "data": {"booking_dates": [1785283200, "2026-07-29"]}}),
        (200, {"success": True, "data": [
            {"id": 6544, "bookable": True}, {"id": "77", "bookable": True},
        ]}),
        (200, {"success": True, "data": []}),
        (200, {"success": True, "data": [
            {"datetime": "2026-07-29T11:59:00+03:00", "seance_length": 3600},
            {"datetime": "2026-07-29T12:00:00+03:00", "seance_length": 3600},
            {"datetime": "2026-07-29T13:00:00+03:00", "seance_length": 3600},
        ]}),
    ])
    timezone = ZoneInfo("Europe/Moscow")

    slots = await YclientsAdapter(_config(server)).list_slots(SlotQuery(
        ("331",),
        datetime(2026, 7, 29, 12, 0, tzinfo=timezone),
        datetime(2026, 7, 29, 13, 0, tzinfo=timezone),
    ))

    assert [(slot.staff_id, slot.starts_at.isoformat()) for slot in slots] == [
        ("6544", "2026-07-29T12:00:00+03:00"),
    ]
    assert len([request for request in server.requests if "/book_times/" in request[1]]) == 2


@pytest.mark.asyncio
async def test_requested_staff_is_sent_and_nonmatching_staff_is_not_queried(
    server: ScriptedServer,
) -> None:
    server.responses.extend([
        (200, {"success": True, "data": {"booking_dates": ["2026-07-29"]}}),
        (200, {"success": True, "data": [
            {"id": 6544, "bookable": True}, {"id": 77, "bookable": True},
        ]}),
        (200, {"success": True, "data": []}),
    ])
    timezone = ZoneInfo("Europe/Moscow")

    assert await YclientsAdapter(_config(server)).list_slots(SlotQuery(
        ("331",),
        datetime(2026, 7, 29, tzinfo=timezone),
        datetime(2026, 7, 30, tzinfo=timezone),
        staff_id="6544",
    )) == []

    assert ("staff_id", "6544") in parse_qsl(urlsplit(server.requests[0][1]).query)
    assert ("date_to", "2026-07-30") in parse_qsl(urlsplit(server.requests[0][1]).query)
    assert [urlsplit(request[1]).path for request in server.requests if "/book_times/" in request[1]] == [
        "/api/v1/book_times/123/6544/2026-07-29"
    ]


@pytest.mark.asyncio
async def test_iso_booking_datetime_is_converted_to_branch_date(server: ScriptedServer) -> None:
    server.responses.extend([
        (200, {"success": True, "data": {"booking_dates": ["2026-07-28T22:00:00Z"]}}),
        (200, {"success": True, "data": [{"id": 6544, "bookable": True}]}),
        (200, {"success": True, "data": []}),
    ])
    timezone = ZoneInfo("Europe/Moscow")

    await YclientsAdapter(_config(server)).list_slots(SlotQuery(
        ("331",),
        datetime(2026, 7, 29, tzinfo=timezone),
        datetime(2026, 7, 30, tzinfo=timezone),
    ))

    assert urlsplit(server.requests[2][1]).path.endswith("/6544/2026-07-29")


@pytest.mark.asyncio
async def test_unbookable_staff_is_not_queried(server: ScriptedServer) -> None:
    server.responses.extend([
        (200, {"success": True, "data": {"booking_dates": ["2026-07-29"]}}),
        (200, {"success": True, "data": [
            {"id": 77, "bookable": False}, {"id": 6544, "bookable": True},
        ]}),
        (200, {"success": True, "data": []}),
    ])
    timezone = ZoneInfo("Europe/Moscow")

    await YclientsAdapter(_config(server)).list_slots(SlotQuery(
        ("331",),
        datetime(2026, 7, 29, tzinfo=timezone),
        datetime(2026, 7, 30, tzinfo=timezone),
    ))

    assert [urlsplit(request[1]).path for request in server.requests if "/book_times/" in request[1]] == [
        "/api/v1/book_times/123/6544/2026-07-29"
    ]


@pytest.mark.asyncio
async def test_service_ids_use_exact_endpoint_encoding_and_canonical_slot(server: ScriptedServer) -> None:
    one_flow = [
        (200, {"success": True, "data": {"booking_dates": ["2026-07-29"]}}),
        (200, {"success": True, "data": [{"id": 6544, "bookable": True}]}),
        (200, {"success": True, "data": [
            {"datetime": 1785315600, "seance_length": 3600},
        ]}),
    ]
    server.responses.extend(one_flow + one_flow)
    timezone = ZoneInfo("Europe/Moscow")
    config = _config(server)

    first = await YclientsAdapter(config).list_slots(SlotQuery(
        ("332", "331", "332"),
        datetime(2026, 7, 29, tzinfo=timezone),
        datetime(2026, 7, 30, tzinfo=timezone),
    ))
    second = await YclientsAdapter(config).list_slots(SlotQuery(
        ("331", "332"),
        datetime(2026, 7, 29, tzinfo=timezone),
        datetime(2026, 7, 30, tzinfo=timezone),
    ))

    assert first[0].id == second[0].id
    for index, request in enumerate(server.requests):
        service_query = [pair for pair in parse_qsl(urlsplit(request[1]).query) if pair[0] == "service_ids"]
        if index % 3 == 1:
            assert service_query == [("service_ids", "331,332")]
        else:
            assert service_query == [("service_ids", "331"), ("service_ids", "332")]


@pytest.mark.asyncio
async def test_book_dates_are_filtered_before_time_fanout(server: ScriptedServer) -> None:
    server.responses.extend([
        (200, {"success": True, "data": {"booking_dates": [
            "2026-07-28", "2026-07-29", "2026-07-30",
        ]}}),
        (200, {"success": True, "data": [{"id": 6544, "bookable": True}]}),
        (200, {"success": True, "data": []}),
        (200, {"success": True, "data": []}),
        (200, {"success": True, "data": []}),
    ])
    timezone = ZoneInfo("Europe/Moscow")

    await YclientsAdapter(_config(server)).list_slots(SlotQuery(
        ("331",),
        datetime(2026, 7, 29, 11, tzinfo=timezone),
        datetime(2026, 7, 30, tzinfo=timezone),
    ))

    assert [urlsplit(request[1]).path for request in server.requests if "/book_times/" in request[1]] == [
        "/api/v1/book_times/123/6544/2026-07-29"
    ]


@pytest.mark.asyncio
async def test_open_ended_availability_fails_before_http(server: ScriptedServer) -> None:
    timezone = ZoneInfo("Europe/Moscow")

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).list_slots(SlotQuery(
            ("331",), datetime(2026, 7, 29, tzinfo=timezone)
        ))

    assert server.requests == []


@pytest.mark.asyncio
async def test_fractional_booking_date_epoch_fails_closed(server: ScriptedServer) -> None:
    server.responses.extend([
        (200, {"success": True, "data": {"booking_dates": [1785315600.5]}}),
        (200, {"success": True, "data": []}),
    ])
    timezone = ZoneInfo("Europe/Moscow")

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).list_slots(SlotQuery(
            ("331",),
            datetime(2026, 7, 29, tzinfo=timezone),
            datetime(2026, 7, 30, tzinfo=timezone),
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("service_ids,staff_id", [((), None), (("0",), None), (("abc",), None), (("1",), "-1")])
async def test_availability_rejects_invalid_provider_ids_without_http(
    server: ScriptedServer, service_ids: tuple[str, ...], staff_id: str | None,
) -> None:
    timezone = ZoneInfo("Europe/Moscow")

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).list_slots(SlotQuery(
            service_ids, datetime(2026, 7, 29, tzinfo=timezone), staff_id=staff_id
        ))

    assert server.requests == []


@pytest.mark.asyncio
async def test_invalid_slot_marker_fails_closed_before_http(server: ScriptedServer) -> None:
    command = CreateBooking("customer-7", "yclients:v1:not-json", "key", "Name", "+70000000000", True)

    with pytest.raises(SlotUnavailable):
        await YclientsAdapter(_config(server)).create_booking(command)

    assert server.requests == []


@pytest.mark.asyncio
async def test_signed_slot_rejects_forgery_company_mismatch_and_noncanonical_services(
    server: ScriptedServer,
) -> None:
    config = _config(server)
    valid = _slot_id(config)
    payload, tag = valid.removeprefix("yclients:v1:").split(".")
    raw = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    raw["start"] += 60
    forged_payload = base64.urlsafe_b64encode(json.dumps(
        raw, separators=(",", ":"), sort_keys=True,
    ).encode()).decode().rstrip("=")
    forged = f"yclients:v1:{forged_payload}.{tag}"
    other_company = YclientsConfig(
        base_url=config.base_url,
        partner_token=config.partner_token,
        user_token=config.user_token,
        company_id=124,
    )

    for adapter, slot_id in (
        (YclientsAdapter(config), forged),
        (YclientsAdapter(other_company), valid),
        (YclientsAdapter(config), _slot_id(config, services=[331, 331])),
        (YclientsAdapter(config), _slot_id(config, start=1785315600.5)),
    ):
        with pytest.raises(SlotUnavailable):
            await adapter.create_booking(CreateBooking(
                "customer-7", slot_id, "key", "Name", "+70000000000", True
            ))

    assert server.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("customer_id", " "),
    ("customer_name", " "),
    ("customer_phone", " "),
    ("personal_data_processing_allowed", False),
])
async def test_create_validates_consent_and_contact_before_http(
    server: ScriptedServer, field: str, value: object,
) -> None:
    values: dict[str, object] = {
        "customer_id": "customer-7",
        "customer_name": "Name",
        "customer_phone": "+70000000000",
        "personal_data_processing_allowed": True,
    }
    values[field] = value

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).create_booking(CreateBooking(
            customer_id=values["customer_id"],
            slot_id="invalid-is-never-decoded",
            idempotency_key="key",
            customer_name=values["customer_name"],
            customer_phone=values["customer_phone"],
            personal_data_processing_allowed=values["personal_data_processing_allowed"],
        ))

    assert server.requests == []


@pytest.mark.asyncio
async def test_book_check_conflict_is_slot_unavailable_without_create(server: ScriptedServer) -> None:
    slot_id = _slot_id(_config(server))
    server.responses.append((422, {"success": False, "meta": {"code": 433}}))

    with pytest.raises(SlotUnavailable):
        await YclientsAdapter(_config(server)).create_booking(CreateBooking(
            "customer-7", slot_id, "key", "Name", "+70000000000", True
        ))

    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_malformed_create_success_is_outcome_unknown(server: ScriptedServer) -> None:
    slot_id = _slot_id(_config(server))
    server.responses.extend([
        (201, {"success": True, "data": {}}),
        (201, {"success": True, "data": []}),
    ])

    with pytest.raises(BookingOutcomeUnknown):
        await YclientsAdapter(_config(server)).create_booking(CreateBooking(
            "customer-7", slot_id, "key", "Name", "+70000000000", True
        ))

    assert len(server.requests) == 2


@pytest.mark.asyncio
async def test_deleted_create_success_is_outcome_unknown(server: ScriptedServer) -> None:
    slot_id = _slot_id(_config(server))
    server.responses.extend([
        (201, {"success": True, "data": {}}),
        (201, {"success": True, "data": _record(deleted=True)}),
    ])

    with pytest.raises(BookingOutcomeUnknown):
        await YclientsAdapter(_config(server)).create_booking(CreateBooking(
            "customer-7", slot_id, "key", "Name", "+70000000000", True
        ))

    assert len(server.requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("api_id", ["foreign", "moroz:v1:!!!", "moroz:v1:"])
async def test_get_rejects_invalid_owner_marker(server: ScriptedServer, api_id: str) -> None:
    server.responses.append((200, {"success": True, "data": _record(api_id=api_id)}))

    with pytest.raises(BookingNotFound):
        await YclientsAdapter(_config(server)).get_booking("9001")


@pytest.mark.asyncio
async def test_get_deleted_record_is_cancelled(server: ScriptedServer) -> None:
    server.responses.append((200, {"success": True, "data": [_record(deleted=True)]}))

    booking = await YclientsAdapter(_config(server)).get_booking("9001")

    assert booking.status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [1, "true", {}])
async def test_get_rejects_non_boolean_deleted(
    server: ScriptedServer, deleted: object,
) -> None:
    server.responses.append((200, {
        "success": True, "data": [_record(deleted=deleted)],
    }))

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).get_booking("9001")


@pytest.mark.asyncio
async def test_reschedule_rejects_non_boolean_deleted_before_mutation(
    server: ScriptedServer,
) -> None:
    server.responses.append((200, {
        "success": True,
        "data": _record(
            deleted=1,
            client={"name": "Name", "phone": "+70000000000"},
        ),
    }))
    config = _config(server)

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(config).reschedule_booking(
            RescheduleBooking("9001", _slot_id(config), "key")
        )

    assert [request[0] for request in server.requests] == ["GET"]


@pytest.mark.asyncio
async def test_get_rejects_record_with_different_external_id(server: ScriptedServer) -> None:
    server.responses.append((200, {"success": True, "data": _record()}))

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).get_booking("9002")


@pytest.mark.asyncio
@pytest.mark.parametrize("record_id", [True, 9001.5, [9001]])
async def test_get_rejects_non_numeric_record_id(
    server: ScriptedServer, record_id: object,
) -> None:
    server.responses.append((200, {"success": True, "data": _record(id=record_id)}))

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).get_booking(str(record_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"seance_length": 3600.9},
    {"datetime": "2026-07-29"},
    {"datetime": 1785315600.5},
])
async def test_get_rejects_lossy_duration_and_date_only_datetime(
    server: ScriptedServer, changes: dict[str, object],
) -> None:
    server.responses.append((200, {"success": True, "data": _record(**changes)}))

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).get_booking("9001")
    assert sum(item[0] == "GET" for item in server.requests) == 1


@pytest.mark.asyncio
async def test_create_with_malformed_record_id_is_outcome_unknown(server: ScriptedServer) -> None:
    slot_id = _slot_id(_config(server))
    server.responses.extend([
        (201, {"success": True, "data": {}}),
        (201, {"success": True, "data": _record(id=True)}),
    ])

    with pytest.raises(BookingOutcomeUnknown):
        await YclientsAdapter(_config(server)).create_booking(CreateBooking(
            "customer-7", slot_id, "key", "Name", "+70000000000", True
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("external_id", [True, "0", "-1", "9001.5", "../9001"])
async def test_get_rejects_invalid_external_id_before_http(
    server: ScriptedServer, external_id: object,
) -> None:
    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).get_booking(external_id)

    assert server.requests == []


@pytest.mark.asyncio
async def test_malformed_read_and_success_false_fail_closed(server: ScriptedServer) -> None:
    server.responses.extend([
        (200, {"success": False, "data": _record()}),
        (200, b"not-json"),
    ])

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).get_booking("9001")
    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).get_booking("9001")
    assert sum(item[0] == "GET" for item in server.requests) == 2


@pytest.mark.asyncio
async def test_reschedule_uses_protected_get_check_put_and_preserves_minimum_record(
    server: ScriptedServer,
) -> None:
    config = _config(server)
    target_slot = _slot_id(config, staff=77, start=1785322800, duration=1800)
    current = _record(
        client={"name": "Sandbox Customer", "phone": "+70000000000"},
        comment="keep me",
    )
    changed = _record(
        staff_id=77,
        datetime="2026-07-29T14:00:00+03:00",
        seance_length=1800,
        client=current["client"],
        comment="keep me",
    )
    server.responses.extend([
        (200, {"success": True, "data": current}),
        (201, {"success": True, "data": {}}),
        (201, {"success": True, "data": changed}),
    ])

    booking = await YclientsAdapter(config).reschedule_booking(
        RescheduleBooking("9001", target_slot, "local-key-only")
    )

    assert booking.slot_id == target_slot
    assert [urlsplit(item[1]).path for item in server.requests] == [
        "/api/v1/record/123/9001",
        "/api/v1/book_check/123",
        "/api/v1/record/123/9001",
    ]
    assert [item[0] for item in server.requests] == ["GET", "POST", "PUT"]
    assert server.requests[0][2]["Authorization"] == "Bearer partner-value, User user-value"
    assert server.requests[1][2]["Authorization"] == "Bearer partner-value"
    assert server.requests[2][2]["Authorization"] == "Bearer partner-value, User user-value"
    assert server.requests[2][3] == {
        "staff_id": 77,
        "services": [{"id": 331}],
        "client": {"name": "Sandbox Customer", "phone": "+70000000000"},
        "save_if_busy": False,
        "datetime": "2026-07-29 14:00:00",
        "seance_length": 1800,
        "send_sms": False,
        "comment": "keep me",
        "attendance": 0,
        "api_id": "moroz:v1:Y3VzdG9tZXItNw",
    }
    assert all("Idempotency-Key" not in item[2] for item in server.requests)
    assert "local-key-only" not in json.dumps(server.requests)


@pytest.mark.asyncio
async def test_reschedule_rejects_foreign_owner_before_check_or_put(server: ScriptedServer) -> None:
    config = _config(server)
    server.responses.append((200, {"success": True, "data": _record(api_id="foreign")}))

    with pytest.raises(BookingNotFound):
        await YclientsAdapter(config).reschedule_booking(
            RescheduleBooking("9001", _slot_id(config), "key")
        )

    assert [item[0] for item in server.requests] == ["GET"]


@pytest.mark.asyncio
async def test_reschedule_rejects_cancelled_current_before_check_or_put(
    server: ScriptedServer,
) -> None:
    config = _config(server)
    server.responses.append((200, {"success": True, "data": _record(
        deleted=True,
        client={"name": "Name", "phone": "+70000000000"},
    )}))

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(config).reschedule_booking(
            RescheduleBooking("9001", _slot_id(config), "key")
        )

    assert [item[0] for item in server.requests] == ["GET"]


@pytest.mark.asyncio
async def test_reschedule_cancelled_put_response_is_outcome_unknown(
    server: ScriptedServer,
) -> None:
    config = _config(server)
    current = _record(client={"name": "Name", "phone": "+70000000000"})
    server.responses.extend([
        (200, {"success": True, "data": current}),
        (201, {"success": True, "data": {}}),
        (201, {"success": True, "data": _record(deleted=True)}),
    ])

    with pytest.raises(BookingOutcomeUnknown):
        await YclientsAdapter(config).reschedule_booking(
            RescheduleBooking("9001", _slot_id(config), "key")
        )

    assert sum(item[0] == "PUT" for item in server.requests) == 1


@pytest.mark.asyncio
async def test_cancel_sends_one_protected_delete_and_accepts_only_204(server: ScriptedServer) -> None:
    server.responses.append((204, b""))

    await YclientsAdapter(_config(server)).cancel_booking(CancelBooking("9001", "local-key-only"))

    assert [(item[0], urlsplit(item[1]).path) for item in server.requests] == [
        ("DELETE", "/api/v1/record/123/9001")
    ]
    assert server.requests[0][2]["Authorization"] == "Bearer partner-value, User user-value"
    assert "Idempotency-Key" not in server.requests[0][2]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [433, 436, 437, 438])
async def test_book_check_conflicts_are_slot_unavailable(code: int, server: ScriptedServer) -> None:
    config = _config(server)
    server.responses.append((422, {
        "success": False,
        "meta": {"errors": [{"code": code, "message": "slot conflict"}]},
    }))

    with pytest.raises(SlotUnavailable):
        await YclientsAdapter(config).create_booking(CreateBooking(
            "customer-7", _slot_id(config), "key", "Name", "+70000000000", True
        ))

    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_book_check_5xx_wins_over_embedded_conflict_code(server: ScriptedServer) -> None:
    config = _config(server)
    server.responses.append((500, {
        "success": False,
        "meta": {"errors": [{"code": 433, "message": "ambiguous server failure"}]},
    }))

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(config).create_booking(CreateBooking(
            "customer-7", _slot_id(config), "key", "Name", "+70000000000", True
        ))

    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_create_connection_drop_is_outcome_unknown_and_not_retried(
    server: ScriptedServer,
) -> None:
    config = _config(server)
    server.responses.extend([
        (201, {"success": True, "data": {}}),
        (None, b""),
    ])

    with pytest.raises(BookingOutcomeUnknown):
        await YclientsAdapter(config).create_booking(CreateBooking(
            "customer-7", _slot_id(config), "key", "Name", "+70000000000", True
        ))

    assert sum(item[0] == "POST" and urlsplit(item[1]).path == "/api/v1/records/123" for item in server.requests) == 1


@pytest.mark.asyncio
async def test_put_500_is_outcome_unknown_and_not_retried(server: ScriptedServer) -> None:
    config = _config(server)
    server.responses.extend([
        (200, {"success": True, "data": _record(
            client={"name": "Name", "phone": "+70000000000"}, comment="safe",
        )}),
        (201, {"success": True, "data": {}}),
        (500, {"success": False}),
    ])

    with pytest.raises(BookingOutcomeUnknown):
        await YclientsAdapter(config).reschedule_booking(
            RescheduleBooking("9001", _slot_id(config), "key")
        )

    assert sum(item[0] == "PUT" for item in server.requests) == 1


@pytest.mark.asyncio
async def test_malformed_put_success_is_outcome_unknown(server: ScriptedServer) -> None:
    config = _config(server)
    server.responses.extend([
        (200, {"success": True, "data": _record(
            client={"name": "Name", "phone": "+70000000000"}, comment="safe",
        )}),
        (201, {"success": True, "data": {}}),
        (201, {"success": True, "data": []}),
    ])

    with pytest.raises(BookingOutcomeUnknown):
        await YclientsAdapter(config).reschedule_booking(
            RescheduleBooking("9001", _slot_id(config), "key")
        )

    assert sum(item[0] == "PUT" for item in server.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 409, 422, 429])
async def test_definite_cancel_rejection_is_temporary(status: int, server: ScriptedServer) -> None:
    server.responses.append((status, {"success": False}))

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).cancel_booking(CancelBooking("9001", "key"))

    assert sum(item[0] == "DELETE" for item in server.requests) == 1


@pytest.mark.asyncio
async def test_cancel_404_is_not_found(server: ScriptedServer) -> None:
    server.responses.append((404, {"success": False}))

    with pytest.raises(BookingNotFound):
        await YclientsAdapter(_config(server)).cancel_booking(CancelBooking("9001", "key"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 500])
async def test_cancel_unexpected_or_server_status_is_outcome_unknown(
    status: int, server: ScriptedServer,
) -> None:
    server.responses.append((status, {"success": status == 200}))

    with pytest.raises(BookingOutcomeUnknown):
        await YclientsAdapter(_config(server)).cancel_booking(CancelBooking("9001", "key"))

    assert sum(item[0] == "DELETE" for item in server.requests) == 1


@pytest.mark.asyncio
async def test_create_definite_429_is_temporary_without_retry(server: ScriptedServer) -> None:
    config = _config(server)
    server.responses.extend([
        (201, {"success": True, "data": {}}),
        (429, {"success": False}),
    ])

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(config).create_booking(CreateBooking(
            "customer-7", _slot_id(config), "key", "Name", "+70000000000", True
        ))

    assert sum(item[0] == "POST" and "/records/" in item[1] for item in server.requests) == 1


@pytest.mark.asyncio
async def test_delete_connection_drop_is_outcome_unknown_and_not_retried(
    server: ScriptedServer,
) -> None:
    server.responses.append((None, b""))

    with pytest.raises(BookingOutcomeUnknown):
        await YclientsAdapter(_config(server)).cancel_booking(CancelBooking("9001", "key"))

    assert sum(item[0] == "DELETE" for item in server.requests) == 1


@pytest.mark.asyncio
async def test_read_transport_failure_is_temporary_and_not_retried(server: ScriptedServer) -> None:
    server.responses.append((None, b""))

    with pytest.raises(BookingTemporaryError):
        await YclientsAdapter(_config(server)).get_booking("9001")

    assert sum(item[0] == "GET" for item in server.requests) == 1
