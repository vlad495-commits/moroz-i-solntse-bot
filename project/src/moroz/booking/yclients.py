import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from moroz.booking.models import (
    BookingNotFound,
    BookingOutcomeUnknown,
    BookingTemporaryError,
    BookingStatus,
    CancelBooking,
    CreateBooking,
    ExternalBooking,
    GetBooking,
    RescheduleBooking,
    Slot,
    SlotQuery,
    SlotUnavailable,
)
from moroz.booking.ports import BookingPort
from moroz.booking.yclients_http import (
    HttpResponse,
    YclientsConfig,
    YclientsHttpClient,
    YclientsTransportError,
)


_SLOT_PREFIX = "yclients:v1:"
_BOOKING_KEY_FIELD = "moroz_booking_key"
_CUSTOMER_ID_FIELD = "moroz_customer_id"
_SLOT_KEYS = {"services", "staff", "start", "duration"}
_CONFLICT_CODES = {433, 436, 437, 438}
_DEFINITE_MUTATION_REJECTIONS = {400, 401, 403, 409, 422, 429}
_LOOKUP_PAGE_SIZE = 100
_LOOKUP_MAX_PAGES = 20


@dataclass(frozen=True, slots=True)
class _SlotPayload:
    services: tuple[int, ...]
    staff: int
    start: int
    duration: int


class YclientsAvailabilityAdapter:
    def __init__(
        self,
        config: YclientsConfig,
        *,
        http: YclientsHttpClient | None = None,
    ) -> None:
        self._config = config
        self._http = http or YclientsHttpClient(config)
        self._timezone = ZoneInfo(config.timezone_name)

    async def list_slots(self, query: SlotQuery) -> list[Slot]:
        services = _provider_ids(query.service_ids)
        staff_filter = _provider_id(query.staff_id) if query.staff_id is not None else None
        if query.starts_before is None:
            raise BookingTemporaryError()
        local_after = query.starts_after.astimezone(self._timezone)
        local_before = query.starts_before.astimezone(self._timezone)
        dates_query: list[tuple[str, object]] = [("service_ids", value) for value in services]
        dates_query.append(("date_from", local_after.date().isoformat()))
        dates_query.append(("date_to", local_before.date().isoformat()))
        if staff_filter is not None:
            dates_query.append(("staff_id", staff_filter))

        dates_data = await self._read(
            f"/api/v1/book_dates/{self._config.company_id}",
            query=dates_query,
        )
        dates = _booking_dates(dates_data, self._timezone)
        staff_data = await self._read(
            f"/api/v1/book_staff/{self._config.company_id}",
            query=[("service_ids[]", value) for value in services],
        )
        if (
            staff_filter is not None
            and _staff_occurrences(staff_data, staff_filter) != 1
        ):
            raise BookingTemporaryError()
        staff_ids = _staff_ids(staff_data)
        if staff_filter is not None:
            staff_ids = [value for value in staff_ids if value == staff_filter]

        slots: dict[str, Slot] = {}
        for day in _relevant_dates(dates, local_after, local_before, self._timezone):
            for staff_id in staff_ids:
                times_data = await self._read(
                    f"/api/v1/book_times/{self._config.company_id}/{staff_id}/{day.isoformat()}",
                    query=[("service_ids", value) for value in services],
                )
                for item in _items(times_data):
                    starts_at = _datetime(item.get("datetime"), self._timezone)
                    duration = _positive_int(item.get("seance_length"))
                    if duration % 60:
                        raise BookingTemporaryError()
                    if starts_at < query.starts_after or (
                        query.starts_before is not None and starts_at >= query.starts_before
                    ):
                        continue
                    payload = _SlotPayload(tuple(services), staff_id, int(starts_at.timestamp()), duration)
                    slot_id = _encode_slot(payload, self._config)
                    slots[slot_id] = Slot(
                        slot_id,
                        tuple(str(value) for value in services),
                        str(staff_id),
                        starts_at,
                        duration // 60,
                    )
        return sorted(slots.values(), key=lambda value: (value.starts_at, int(value.staff_id), value.id))

    async def _read(
        self,
        path: str,
        *,
        query: list[tuple[str, object]],
    ) -> object:
        try:
            response = await self._http.request("GET", path, query=query)
        except YclientsTransportError as error:
            raise BookingTemporaryError() from error
        if response.status != 200:
            raise BookingTemporaryError()
        return _envelope(response)


class YclientsAdapter(YclientsAvailabilityAdapter, BookingPort):
    async def create_booking(self, command: CreateBooking) -> ExternalBooking:
        customer_id = _required_text(command.customer_id)
        customer_name = _required_text(command.customer_name)
        customer_phone = _required_text(command.customer_phone)
        if command.personal_data_processing_allowed is not True:
            raise BookingTemporaryError()
        payload = _decode_slot(command.slot_id, self._config)
        await self._book_check(payload)
        body: dict[str, object] = {
            "staff_id": payload.staff,
            "services": [{"id": value} for value in payload.services],
            "client": {"name": customer_name, "phone": customer_phone},
            "save_if_busy": False,
            "datetime": datetime.fromtimestamp(payload.start, self._timezone).strftime("%Y-%m-%d %H:%M:%S"),
            "seance_length": payload.duration,
            "send_sms": False,
            "attendance": 0,
            "custom_fields": {
                _BOOKING_KEY_FIELD: str(command.booking_key),
                _CUSTOMER_ID_FIELD: customer_id,
            },
            "client_agreements": {
                "is_personal_data_processing_allowed": True,
                "is_newsletter_allowed": False,
            },
        }
        if command.comment is not None:
            body["comment"] = command.comment
        try:
            response = await self._http.request(
                "POST",
                f"/api/v1/records/{self._config.company_id}",
                json_body=body,
                user_auth=True,
            )
        except YclientsTransportError as error:
            raise _outcome_unknown("transport") from error
        if response.status in {400, 401, 403, 404, 409, 422, 429}:
            raise BookingTemporaryError()
        if response.status != 201:
            raise _outcome_unknown("http_status", status=response.status)
        try:
            record = _record(_envelope(response))
            _require_booking_key(record, command.booking_key)
            booking = _external_booking(
                record,
                self._timezone,
                self._config,
                customer_id,
                command.booking_key,
            )
        except (BookingNotFound, BookingTemporaryError, ValueError, TypeError, KeyError) as error:
            raise _outcome_unknown("response_shape", status=response.status) from error
        if (
            booking.customer_id != customer_id
            or booking.slot_id != command.slot_id
            or booking.status != "confirmed"
        ):
            raise _outcome_unknown("response_shape", status=response.status)
        return booking

    async def get_booking(self, command: GetBooking) -> ExternalBooking:
        provider_id = _provider_id(command.external_id)
        try:
            response = await self._http.request(
                "GET",
                f"/api/v1/record/{self._config.company_id}/{provider_id}",
                user_auth=True,
            )
        except YclientsTransportError as error:
            raise BookingTemporaryError() from error
        if response.status == 404:
            raise BookingNotFound()
        if response.status != 200:
            raise BookingTemporaryError()
        try:
            record = _record(_envelope(response))
            _require_booking_key(record, command.booking_key)
            booking = _external_booking(
                record,
                self._timezone,
                self._config,
                command.customer_id,
                command.booking_key,
            )
            if booking.external_id != str(provider_id):
                raise BookingTemporaryError()
            return booking
        except BookingNotFound:
            raise
        except (BookingTemporaryError, ValueError, TypeError, KeyError) as error:
            raise BookingTemporaryError() from error

    async def find_by_booking_key(
        self,
        booking_key: UUID,
    ) -> list[ExternalBooking]:
        matches: list[ExternalBooking] = []
        for page in range(1, _LOOKUP_MAX_PAGES + 1):
            try:
                response = await self._http.request(
                    "GET",
                    f"/api/v1/records/{self._config.company_id}",
                    query=[
                        ("page", page),
                        ("count", _LOOKUP_PAGE_SIZE),
                        ("with_deleted", 1),
                    ],
                    user_auth=True,
                )
            except YclientsTransportError as error:
                raise BookingTemporaryError() from error
            if response.status != 200:
                raise BookingTemporaryError()
            try:
                data = _envelope(response)
            except (BookingTemporaryError, ValueError, TypeError, KeyError) as error:
                raise BookingTemporaryError() from error
            if not isinstance(data, list) or any(
                not isinstance(item, Mapping) for item in data
            ):
                raise BookingTemporaryError()
            for item in data:
                fields = item.get("custom_fields")
                if not isinstance(fields, Mapping):
                    continue
                if fields.get(_BOOKING_KEY_FIELD) != str(booking_key):
                    continue
                customer_id = fields.get(_CUSTOMER_ID_FIELD)
                if (
                    not isinstance(customer_id, str)
                    or not customer_id
                    or customer_id != customer_id.strip()
                ):
                    raise BookingTemporaryError()
                try:
                    matches.append(
                        _external_booking(
                            item,
                            self._timezone,
                            self._config,
                            customer_id,
                            booking_key,
                        )
                    )
                except (
                    BookingNotFound,
                    BookingTemporaryError,
                    ValueError,
                    TypeError,
                    KeyError,
                ) as error:
                    raise BookingTemporaryError() from error
            if len(data) < _LOOKUP_PAGE_SIZE:
                return matches
        raise BookingTemporaryError()

    async def reschedule_booking(self, command: RescheduleBooking) -> ExternalBooking:
        provider_id = _provider_id(command.external_id)
        record = await self._get_record(provider_id)
        try:
            _require_booking_key(record, command.booking_key)
            current = _external_booking(
                record,
                self._timezone,
                self._config,
                command.customer_id,
                command.booking_key,
            )
        except BookingNotFound:
            raise
        except (BookingTemporaryError, ValueError, TypeError, KeyError) as error:
            raise BookingTemporaryError() from error
        if current.external_id != str(provider_id) or current.status != "confirmed":
            raise BookingTemporaryError()

        payload = _decode_slot(command.slot_id, self._config)
        client = _record_client(record)
        custom_fields = dict(record["custom_fields"])
        custom_fields[_BOOKING_KEY_FIELD] = str(command.booking_key)
        await self._book_check(payload)
        body: dict[str, object] = {
            "staff_id": payload.staff,
            "services": [{"id": value} for value in payload.services],
            "client": client,
            "save_if_busy": False,
            "datetime": datetime.fromtimestamp(payload.start, self._timezone).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "seance_length": payload.duration,
            "send_sms": False,
            "attendance": 0,
            "custom_fields": custom_fields,
        }
        if "comment" in record and record["comment"] is not None:
            if not isinstance(record["comment"], str):
                raise BookingTemporaryError()
            body["comment"] = record["comment"]

        try:
            response = await self._http.request(
                "PUT",
                f"/api/v1/record/{self._config.company_id}/{provider_id}",
                json_body=body,
                user_auth=True,
            )
        except YclientsTransportError as error:
            raise _outcome_unknown("transport") from error
        _check_mutation_status(response.status, expected=201)
        try:
            changed_record = _record(_envelope(response))
            _require_booking_key(changed_record, command.booking_key)
            changed = _external_booking(
                changed_record,
                self._timezone,
                self._config,
                command.customer_id,
                command.booking_key,
            )
        except (BookingNotFound, BookingTemporaryError, ValueError, TypeError, KeyError) as error:
            raise _outcome_unknown("response_shape", status=response.status) from error
        if (
            changed.external_id != str(provider_id)
            or changed.customer_id != current.customer_id
            or changed.slot_id != command.slot_id
            or changed.status != "confirmed"
        ):
            raise _outcome_unknown("response_shape", status=response.status)
        return changed

    async def cancel_booking(self, command: CancelBooking) -> None:
        provider_id = _provider_id(command.external_id)
        record = await self._get_record(provider_id)
        _require_booking_key(record, command.booking_key)
        try:
            response = await self._http.request(
                "DELETE",
                f"/api/v1/record/{self._config.company_id}/{provider_id}",
                user_auth=True,
            )
        except YclientsTransportError as error:
            raise _outcome_unknown("transport") from error
        _check_mutation_status(response.status, expected=204)

    async def _get_record(self, provider_id: int) -> dict[str, object]:
        try:
            response = await self._http.request(
                "GET",
                f"/api/v1/record/{self._config.company_id}/{provider_id}",
                user_auth=True,
            )
        except YclientsTransportError as error:
            raise BookingTemporaryError() from error
        if response.status == 404:
            raise BookingNotFound()
        if response.status != 200:
            raise BookingTemporaryError()
        try:
            record = _record(_envelope(response))
            if _provider_id(record.get("id")) != provider_id:
                raise BookingTemporaryError()
            return record
        except BookingNotFound:
            raise
        except (BookingTemporaryError, ValueError, TypeError, KeyError) as error:
            raise BookingTemporaryError() from error

    async def _book_check(self, payload: _SlotPayload) -> None:
        try:
            response = await self._http.request(
                "POST",
                f"/api/v1/book_check/{self._config.company_id}",
                json_body={"appointments": [{
                    "id": 1,
                    "services": list(payload.services),
                    "staff_id": payload.staff,
                    "datetime": datetime.fromtimestamp(payload.start, self._timezone).isoformat(),
                }]},
            )
        except YclientsTransportError as error:
            raise BookingTemporaryError() from error
        envelope = _json_or_temporary(response)
        if 500 <= response.status or response.status == 429:
            raise BookingTemporaryError()
        if 400 <= response.status < 500 and _has_conflict_code(envelope):
            raise SlotUnavailable()
        if response.status != 201 or envelope.get("success") is not True:
            raise BookingTemporaryError()

def _provider_ids(values: tuple[str, ...]) -> list[int]:
    if not values:
        raise BookingTemporaryError()
    return sorted({_provider_id(value) for value in values})


def _provider_id(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise BookingTemporaryError() from error
    if isinstance(value, bool) or parsed <= 0 or str(parsed) != str(value):
        raise BookingTemporaryError()
    return parsed


def _positive_int(value: object) -> int:
    return _provider_id(value)


def _compact_b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_b64(value: str) -> bytes:
    if not value:
        raise ValueError
    raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _compact_b64(raw) != value:
        raise ValueError
    return raw


def _slot_tag(raw: bytes, config: YclientsConfig) -> bytes:
    message = b"yclients-slot:v1\0" + str(config.company_id).encode() + b"\0" + raw
    return hmac.new(config.user_token.encode(), message, hashlib.sha256).digest()[:16]


def _encode_slot(payload: _SlotPayload, config: YclientsConfig) -> str:
    raw = json.dumps(
        {
            "services": list(payload.services),
            "staff": payload.staff,
            "start": payload.start,
            "duration": payload.duration,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return _SLOT_PREFIX + _compact_b64(raw) + "." + _compact_b64(_slot_tag(raw, config))


def _decode_slot(value: str, config: YclientsConfig) -> _SlotPayload:
    try:
        if not value.startswith(_SLOT_PREFIX):
            raise ValueError
        encoded_payload, encoded_tag = value[len(_SLOT_PREFIX):].split(".")
        raw = _decode_b64(encoded_payload)
        if not hmac.compare_digest(_decode_b64(encoded_tag), _slot_tag(raw, config)):
            raise ValueError
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != _SLOT_KEYS:
            raise ValueError
        services = payload["services"]
        if not isinstance(services, list) or not services:
            raise ValueError
        numbers = tuple(services)
        if any(type(item) is not int or item <= 0 for item in numbers):
            raise ValueError
        if numbers != tuple(sorted(set(numbers))):
            raise ValueError
        if any(type(payload[key]) is not int or payload[key] <= 0 for key in ("staff", "start", "duration")):
            raise ValueError
        decoded = _SlotPayload(numbers, payload["staff"], payload["start"], payload["duration"])
        if _encode_slot(decoded, config) != value:
            raise ValueError
        datetime.fromtimestamp(decoded.start, UTC)
        return decoded
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError, OverflowError) as error:
        raise SlotUnavailable() from error


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BookingTemporaryError()
    return value.strip()


def _record_client(record: dict[str, object]) -> dict[str, str]:
    client = record.get("client")
    if not isinstance(client, dict):
        raise BookingTemporaryError()
    name = client.get("name")
    phone = client.get("phone")
    if (
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(phone, str)
        or not phone.strip()
    ):
        raise BookingTemporaryError()
    return {"name": name, "phone": phone}


def _check_mutation_status(status: int, *, expected: int) -> None:
    if status == expected:
        return
    if status == 404:
        raise BookingNotFound()
    if status in _DEFINITE_MUTATION_REJECTIONS:
        raise BookingTemporaryError()
    raise _outcome_unknown("http_status", status=status)


def _outcome_unknown(kind: str, *, status: int | None = None) -> BookingOutcomeUnknown:
    return BookingOutcomeUnknown(kind=kind, status=status)


def _require_booking_key(record: Mapping[str, object], expected: UUID) -> UUID:
    fields = record.get("custom_fields")
    if not isinstance(fields, Mapping):
        raise BookingTemporaryError("booking custom fields are malformed")
    raw = fields.get(_BOOKING_KEY_FIELD)
    try:
        actual = UUID(raw) if isinstance(raw, str) else None
    except ValueError as error:
        raise BookingNotFound("booking ownership marker is invalid") from error
    if actual != expected or raw != str(expected):
        raise BookingNotFound("booking ownership marker does not match")
    return actual


def _json_or_temporary(response: HttpResponse) -> dict[str, object]:
    try:
        value = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BookingTemporaryError() from error
    if not isinstance(value, dict):
        raise BookingTemporaryError()
    return value


def _envelope(response: HttpResponse) -> object:
    value = _json_or_temporary(response)
    if value.get("success") is not True or "data" not in value:
        raise BookingTemporaryError()
    return value["data"]


def _items(data: object) -> list[dict[str, object]]:
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise BookingTemporaryError()
    return data


def _booking_dates(data: object, timezone: ZoneInfo) -> list[date]:
    if isinstance(data, dict):
        data = data.get("booking_dates")
    if not isinstance(data, list):
        raise BookingTemporaryError()
    try:
        return sorted({_date(value, timezone) for value in data})
    except (ValueError, TypeError, OverflowError) as error:
        raise BookingTemporaryError() from error


def _relevant_dates(
    values: list[date],
    starts_after: datetime,
    starts_before: datetime | None,
    timezone: ZoneInfo,
) -> list[date]:
    result: list[date] = []
    for value in values:
        day_start = datetime(value.year, value.month, value.day, tzinfo=timezone)
        if value < starts_after.date():
            continue
        if starts_before is not None and day_start >= starts_before:
            continue
        result.append(value)
    return result


def _date(value: object, timezone: ZoneInfo) -> date:
    if isinstance(value, bool):
        raise ValueError
    if type(value) is int or isinstance(value, str) and value.isdigit():
        return datetime.fromtimestamp(_provider_id(value), timezone).date()
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone)).date()


def _staff_ids(data: object) -> list[int]:
    values = {
        _provider_id(item.get("id"))
        for item in _items(data)
        if item.get("bookable") is True
    }
    return sorted(values)


def _staff_occurrences(data: object, expected: int) -> int:
    return sum(
        _provider_id(item.get("id")) == expected
        for item in _items(data)
        if item.get("bookable") is True
    )


def _datetime(value: object, timezone: ZoneInfo) -> datetime:
    if isinstance(value, bool):
        raise BookingTemporaryError()
    try:
        if type(value) is int or isinstance(value, str) and value.isdigit():
            return datetime.fromtimestamp(_provider_id(value), timezone)
        if not isinstance(value, str) or "T" not in value and " " not in value:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone))
    except (ValueError, TypeError, OverflowError) as error:
        raise BookingTemporaryError() from error


def _record(data: object) -> dict[str, object]:
    if isinstance(data, list):
        if len(data) != 1:
            raise BookingTemporaryError()
        data = data[0]
    if not isinstance(data, dict) or not data:
        raise BookingTemporaryError()
    return data


def _external_booking(
    record: dict[str, object],
    timezone: ZoneInfo,
    config: YclientsConfig,
    customer_id: str,
    booking_key: UUID,
) -> ExternalBooking:
    external_id = str(_provider_id(record["id"]))
    staff_value = record.get("staff_id")
    if staff_value is None and isinstance(record.get("staff"), dict):
        staff_value = record["staff"].get("id")
    staff = _provider_id(staff_value)
    raw_services = record.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise BookingTemporaryError()
    services = tuple(sorted({
        _provider_id(item.get("id") if isinstance(item, dict) else item)
        for item in raw_services
    }))
    starts_at = _datetime(record.get("datetime"), timezone)
    duration = _positive_int(record.get("seance_length"))
    scheduled_end_at = starts_at + timedelta(seconds=duration)
    slot_id = _encode_slot(
        _SlotPayload(services, staff, int(starts_at.timestamp()), duration), config,
    )
    return ExternalBooking(
        external_id=external_id,
        customer_id=customer_id,
        booking_key=booking_key,
        slot_id=slot_id,
        service_ids=tuple(str(value) for value in services),
        staff_id=str(staff),
        starts_at=starts_at,
        status=_visit_status(record),
        scheduled_end_at=scheduled_end_at,
    )


def _visit_status(record: Mapping[str, object]) -> BookingStatus:
    deleted = record.get("deleted", False)
    if type(deleted) is not bool:
        raise BookingTemporaryError()
    if deleted:
        return "cancelled"
    attendance = record.get("attendance")
    if attendance is None:
        return "unknown"
    if type(attendance) is not int:
        raise BookingTemporaryError()
    return {
        -1: "no_show",
        0: "confirmed",
        1: "completed",
        2: "confirmed",
    }.get(attendance, "unknown")


def _has_conflict_code(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            (key == "code" and str(item).isdigit() and int(item) in _CONFLICT_CODES)
            or _has_conflict_code(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_conflict_code(item) for item in value)
    return False
