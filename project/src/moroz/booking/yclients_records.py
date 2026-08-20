import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from moroz.booking.models import BookingStatus
from moroz.booking.yclients_http import (
    HttpResponse,
    YclientsConfig,
    YclientsHttpClient,
    YclientsTransportError,
)


_BOOKING_KEY_FIELD = "moroz_booking_key"
_PAGE_SIZE = 100
_MAX_PAGES = 100
_MAX_SERVICES = 50
_MAX_DISPLAY_LENGTH = 200


@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    external_id: str
    booking_key: UUID | None
    bot_marker_state: Literal["absent", "valid", "invalid"]
    starts_at: datetime
    scheduled_end_at: datetime | None
    status: BookingStatus
    deleted: bool
    client_name: str | None
    staff_name: str | None
    service_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    records: tuple[ProjectionRecord, ...]
    synced_at: datetime


class YclientsProjectionError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class YclientsRecordsReader:
    def __init__(
        self,
        config: YclientsConfig,
        *,
        http: YclientsHttpClient | None = None,
    ) -> None:
        self._config = config
        self._http = http or YclientsHttpClient(config)
        self._timezone = ZoneInfo(config.timezone_name)

    async def read_window(self, now: datetime) -> ProjectionSnapshot:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        local_now = now.astimezone(self._timezone)
        query = (
            ("count", _PAGE_SIZE),
            ("start_date", (local_now - timedelta(days=30)).date().isoformat()),
            ("end_date", (local_now + timedelta(days=90)).date().isoformat()),
            ("with_deleted", 1),
        )
        records: list[ProjectionRecord] = []
        external_ids: set[str] = set()
        for page in range(1, _MAX_PAGES + 1):
            try:
                response = await self._http.request(
                    "GET",
                    f"/api/v1/records/{self._config.company_id}",
                    query=(("page", page),) + query,
                    user_auth=True,
                )
            except YclientsTransportError as error:
                raise YclientsProjectionError("yclients_transport") from error
            if response.status != 200:
                raise YclientsProjectionError("yclients_http_status")
            data = _page_data(response)
            if len(data) > _PAGE_SIZE:
                raise YclientsProjectionError("yclients_response_shape")
            try:
                page_records = tuple(_projection_record(item, self._timezone) for item in data)
            except (TypeError, ValueError, OverflowError) as error:
                raise YclientsProjectionError("yclients_response_shape") from error
            page_ids = {record.external_id for record in page_records}
            if len(page_ids) != len(page_records) or page_ids & external_ids:
                raise YclientsProjectionError("yclients_response_shape")
            external_ids.update(page_ids)
            records.extend(page_records)
            if len(data) < _PAGE_SIZE:
                return ProjectionSnapshot(tuple(records), now)
        raise YclientsProjectionError("yclients_page_bound")


def normalize_visit_status(record: Mapping[str, object]) -> BookingStatus:
    deleted = record.get("deleted", False)
    if type(deleted) is not bool:
        raise ValueError("deleted must be boolean")
    if deleted:
        return "cancelled"
    attendance = record.get("attendance")
    if attendance is None:
        return "unknown"
    if type(attendance) is not int:
        raise ValueError("attendance must be integer")
    return {
        -1: "no_show",
        0: "confirmed",
        1: "completed",
        2: "confirmed",
    }.get(attendance, "unknown")


def _page_data(response: HttpResponse) -> list[object]:
    try:
        envelope = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise YclientsProjectionError("yclients_response_shape") from error
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        raise YclientsProjectionError("yclients_response_shape")
    data = envelope.get("data")
    if not isinstance(data, list):
        raise YclientsProjectionError("yclients_response_shape")
    return data


def _projection_record(value: object, timezone: ZoneInfo) -> ProjectionRecord:
    if not isinstance(value, Mapping):
        raise ValueError("record must be an object")
    external_id = str(_positive_int(value.get("id")))
    if len(external_id) > 64:
        raise ValueError("provider id is too long")
    starts_at = _datetime(value.get("datetime"), timezone)
    duration = value.get("seance_length")
    scheduled_end_at = (
        starts_at + timedelta(seconds=_positive_int(duration)) if duration is not None else None
    )
    marker, marker_state = _booking_marker(value.get("custom_fields", {}))
    client_name = _nested_display(value.get("client"), "name")
    staff_name = _nested_display(value.get("staff"), "name")
    services = value.get("services")
    if services is None:
        services = []
    elif not isinstance(services, list) or len(services) > _MAX_SERVICES:
        raise ValueError("services are malformed")
    service_names = tuple(
        name
        for item in services
        if (name := _nested_display(item, "title")) is not None
    )
    deleted = value.get("deleted", False)
    status = normalize_visit_status(value)
    return ProjectionRecord(
        external_id=external_id,
        booking_key=marker,
        bot_marker_state=marker_state,
        starts_at=starts_at,
        scheduled_end_at=scheduled_end_at,
        status=status,
        deleted=deleted,
        client_name=client_name,
        staff_name=staff_name,
        service_names=service_names,
    )


def _booking_marker(value: object) -> tuple[UUID | None, Literal["absent", "valid", "invalid"]]:
    if not isinstance(value, Mapping):
        raise ValueError("custom fields are malformed")
    raw = value.get(_BOOKING_KEY_FIELD)
    if raw is None:
        return None, "absent"
    if not isinstance(raw, str):
        return None, "invalid"
    try:
        marker = UUID(raw)
    except ValueError:
        return None, "invalid"
    return (marker, "valid") if raw == str(marker) else (None, "invalid")


def _nested_display(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("display object is malformed")
    return _safe_display(value.get(key))


def _safe_display(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("display text is malformed")
    return "".join(char for char in value if char.isprintable()).strip()[:_MAX_DISPLAY_LENGTH] or None


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("positive integer required") from error
    if isinstance(value, bool) or parsed <= 0 or str(parsed) != str(value):
        raise ValueError("positive integer required")
    return parsed


def _datetime(value: object, timezone: ZoneInfo) -> datetime:
    if isinstance(value, bool):
        raise ValueError("datetime is malformed")
    try:
        if type(value) is int or isinstance(value, str) and value.isdigit():
            return datetime.fromtimestamp(_positive_int(value), timezone)
        if not isinstance(value, str) or "T" not in value and " " not in value:
            raise ValueError("datetime is malformed")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone) if parsed.tzinfo is None else parsed.astimezone(timezone)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("datetime is malformed") from error
