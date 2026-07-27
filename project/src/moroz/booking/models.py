from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal
from uuid import UUID


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class SlotQuery:
    service_ids: tuple[str, ...]
    starts_after: datetime
    starts_before: datetime | None = None
    staff_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "service_ids", tuple(self.service_ids))
        _require_aware(self.starts_after)
        if self.starts_before is not None:
            _require_aware(self.starts_before)
            if self.starts_before <= self.starts_after:
                raise ValueError("starts_before must be after starts_after")


@dataclass(frozen=True, slots=True)
class Slot:
    id: str
    service_ids: tuple[str, ...]
    staff_id: str
    starts_at: datetime
    duration_minutes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "service_ids", tuple(self.service_ids))
        _require_aware(self.starts_at)


@dataclass(frozen=True, slots=True)
class CreateBooking:
    customer_id: str
    booking_key: UUID
    slot_id: str
    idempotency_key: str
    customer_name: str
    customer_phone: str
    personal_data_processing_allowed: bool
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class GetBooking:
    external_id: str
    customer_id: str
    booking_key: UUID


@dataclass(frozen=True, slots=True)
class RescheduleBooking:
    external_id: str
    customer_id: str
    booking_key: UUID
    slot_id: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class CancelBooking:
    external_id: str
    customer_id: str
    booking_key: UUID
    idempotency_key: str


BookingStatus = Literal[
    "confirmed", "cancelled", "completed", "no_show", "unknown"
]


@dataclass(frozen=True, slots=True)
class ExternalBooking:
    external_id: str
    customer_id: str
    booking_key: UUID
    slot_id: str
    starts_at: datetime
    status: BookingStatus
    scheduled_end_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware(self.starts_at)
        if self.scheduled_end_at is not None:
            _require_aware(self.scheduled_end_at)
            if self.scheduled_end_at <= self.starts_at:
                raise ValueError("scheduled_end_at must be after starts_at")


@dataclass(frozen=True, slots=True)
class BookingIdentity:
    customer_id: str
    confirmed: bool


@dataclass(frozen=True, slots=True)
class BookingScenario:
    id: UUID
    kind: Literal["create", "reschedule", "cancel"]
    phase: str
    idempotency_key: str
    customer_id: str
    state: Mapping[str, object]
    error_code: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.created_at)
        _require_aware(self.updated_at)
        object.__setattr__(self, "state", _freeze_json(self.state))


@dataclass(frozen=True, slots=True)
class BookingEvent:
    id: UUID
    scenario_id: UUID
    event_type: str
    payload: Mapping[str, object]
    created_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.created_at)
        object.__setattr__(self, "payload", _freeze_json(self.payload))


class SlotUnavailable(Exception):
    pass


class BookingNotFound(Exception):
    pass


class BookingTemporaryError(Exception):
    pass


class BookingOutcomeUnknown(Exception):
    def __init__(
        self,
        *args: object,
        kind: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(*args)
        self.kind = kind
        self.status = status
