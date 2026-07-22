import asyncio
import base64
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from moroz.booking.models import (
    BookingNotFound,
    BookingOutcomeUnknown,
    BookingTemporaryError,
    CancelBooking,
    CreateBooking,
    ExternalBooking,
    RescheduleBooking,
    Slot,
    SlotQuery,
)
from moroz.booking.yclients import YclientsAdapter
from moroz.booking.yclients_http import YclientsConfig, YclientsHttpClient, YclientsTransportError


_PAGE_SIZE = 100
_MAX_RECORD_PAGES = 20


@dataclass(frozen=True, slots=True)
class SandboxSmokeSettings:
    config: YclientsConfig = field(repr=False)
    service_id: str
    customer_name: str = field(repr=False)
    customer_phone: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SandboxSmokeSettings":
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        if env.get("YCLIENTS_SANDBOX_CONSENT", "").strip().lower() != "yes":
            raise ValueError("YCLIENTS_SANDBOX_CONSENT=yes is required")
        service_id = required("YCLIENTS_TEST_SERVICE_ID")
        if not service_id.isdigit() or int(service_id) <= 0 or str(int(service_id)) != service_id:
            raise ValueError("YCLIENTS_TEST_SERVICE_ID must be a positive integer")
        return cls(
            config=YclientsConfig.from_env(env),
            service_id=service_id,
            customer_name=required("YCLIENTS_TEST_NAME"),
            customer_phone=required("YCLIENTS_TEST_PHONE"),
        )


@dataclass(frozen=True, slots=True)
class SmokeResult:
    exit_code: int
    summary: dict[str, object]


class SmokeBackend(Protocol):
    async def list_services(self, service_id: str) -> int: ...
    async def list_slots(self, query: SlotQuery) -> list[Slot]: ...
    async def create_booking(self, command: CreateBooking) -> ExternalBooking: ...
    async def get_booking(self, external_id: str) -> ExternalBooking: ...
    async def reschedule_booking(self, command: RescheduleBooking) -> ExternalBooking: ...
    async def cancel_booking(self, command: CancelBooking) -> None: ...
    async def count_duplicate_marker(
        self, customer_id: str, starts_at: datetime, ends_at: datetime
    ) -> int: ...


class YclientsSmokeBackend:
    def __init__(
        self,
        config: YclientsConfig,
        *,
        http: YclientsHttpClient | None = None,
    ) -> None:
        self._config = config
        self._http = http or YclientsHttpClient(config)
        self._adapter = YclientsAdapter(config, http=self._http)

    async def list_services(self, service_id: str) -> int:
        data = await self._read(
            f"/api/v1/book_services/{self._config.company_id}", user_auth=False
        )
        if not isinstance(data, dict) or not isinstance(data.get("services"), list):
            raise BookingTemporaryError()
        services = data["services"]
        if any(not isinstance(item, dict) for item in services):
            raise BookingTemporaryError()
        matches = [item for item in services if str(item.get("id")) == service_id]
        if len(matches) != 1:
            raise BookingTemporaryError()
        return len(services)

    async def list_slots(self, query: SlotQuery) -> list[Slot]:
        return await self._adapter.list_slots(query)

    async def create_booking(self, command: CreateBooking) -> ExternalBooking:
        return await self._adapter.create_booking(command)

    async def get_booking(self, external_id: str) -> ExternalBooking:
        return await self._adapter.get_booking(external_id)

    async def reschedule_booking(self, command: RescheduleBooking) -> ExternalBooking:
        return await self._adapter.reschedule_booking(command)

    async def cancel_booking(self, command: CancelBooking) -> None:
        await self._adapter.cancel_booking(command)

    async def count_duplicate_marker(
        self, customer_id: str, starts_at: datetime, ends_at: datetime
    ) -> int:
        marker = _owner_marker(customer_id)
        matched = 0
        for page in range(1, _MAX_RECORD_PAGES + 1):
            data = await self._read(
                f"/api/v1/records/{self._config.company_id}",
                user_auth=True,
                query=(
                    ("page", page),
                    ("count", _PAGE_SIZE),
                    ("start_date", starts_at.date().isoformat()),
                    ("end_date", ends_at.date().isoformat()),
                    ("with_deleted", 1),
                ),
            )
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise BookingTemporaryError()
            matched += sum(item.get("api_id") == marker for item in data)
            if len(data) < _PAGE_SIZE:
                return matched
        raise BookingTemporaryError()

    async def _read(
        self,
        path: str,
        *,
        user_auth: bool,
        query: tuple[tuple[str, object], ...] = (),
    ) -> object:
        try:
            response = await self._http.request(
                "GET", path, query=query, user_auth=user_auth
            )
        except YclientsTransportError as error:
            raise BookingTemporaryError() from error
        if response.status != 200:
            raise BookingTemporaryError()
        try:
            envelope = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BookingTemporaryError() from error
        if (
            not isinstance(envelope, dict)
            or envelope.get("success") is not True
            or "data" not in envelope
        ):
            raise BookingTemporaryError()
        return envelope["data"]


class _SmokeFailure(Exception):
    pass


async def run_smoke(
    settings: SandboxSmokeSettings,
    *,
    backend: SmokeBackend | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    uuid_factory: Callable[[], UUID] = uuid4,
) -> SmokeResult:
    actual = backend or YclientsSmokeBackend(settings.config)
    summary = _empty_summary()
    run_id = uuid_factory().hex
    customer_id = f"smoke-{run_id}"
    external_id: str | None = None
    cancel_confirmed = False
    mutation_unknown = False
    instant = now()
    try:
        summary["services_read"] = await actual.list_services(settings.service_id)
        slots = await actual.list_slots(SlotQuery(
            service_ids=(settings.service_id,),
            starts_after=instant + timedelta(days=1),
            starts_before=instant + timedelta(days=14),
        ))
        first, second = _two_distinct_future_slots(slots, instant)
        summary["slots_read"] = len(slots)
        summary["staff_read"] = len({slot.staff_id for slot in slots})

        created = await actual.create_booking(CreateBooking(
            customer_id=customer_id,
            slot_id=first.id,
            idempotency_key=f"yclients-smoke-{run_id}",
            customer_name=settings.customer_name,
            customer_phone=settings.customer_phone,
            personal_data_processing_allowed=True,
            comment=f"moroz sandbox smoke {run_id}",
        ))
        external_id = created.external_id
        _require_booking(created, customer_id, first)
        summary["created"] = "confirmed"
        summary["record_id"] = _redacted_id(external_id)

        fetched = await actual.get_booking(external_id)
        _require_booking(fetched, customer_id, first)
        summary["first_get"] = "confirmed"

        changed = await actual.reschedule_booking(RescheduleBooking(
            external_id=external_id,
            slot_id=second.id,
            idempotency_key=f"yclients-smoke-{run_id}",
        ))
        _require_booking(changed, customer_id, second)
        summary["rescheduled"] = "confirmed"

        fetched = await actual.get_booking(external_id)
        _require_booking(fetched, customer_id, second)
        summary["second_get"] = "confirmed"

        await actual.cancel_booking(CancelBooking(
            external_id=external_id,
            idempotency_key=f"yclients-smoke-{run_id}",
        ))
        cancel_confirmed = True
        summary["cancelled"] = "confirmed"
        try:
            cancelled = await actual.get_booking(external_id)
        except BookingNotFound:
            summary["final_state"] = "deleted"
        else:
            if cancelled.customer_id != customer_id or cancelled.status != "cancelled":
                raise _SmokeFailure("cancelled_record_not_confirmed")
            summary["final_state"] = "cancelled"

        count = await actual.count_duplicate_marker(
            customer_id,
            min(first.starts_at, second.starts_at),
            max(first.starts_at, second.starts_at),
        )
        summary["duplicate_marker_count"] = count
        if count != 1:
            raise _SmokeFailure("duplicate_marker_count_mismatch")
        summary["success"] = True
        return SmokeResult(0, summary)
    except BookingOutcomeUnknown:
        mutation_unknown = True
        summary["manual_review_required"] = True
        summary["error"] = "mutation_outcome_unknown"
    except _SmokeFailure as error:
        summary["error"] = str(error)
    except (BookingNotFound, BookingTemporaryError):
        summary["error"] = "definite_provider_failure"
    except Exception:
        summary["error"] = "unexpected_failure"

    if external_id is not None and not cancel_confirmed and not mutation_unknown:
        try:
            await actual.cancel_booking(CancelBooking(
                external_id=external_id,
                idempotency_key=f"yclients-smoke-cleanup-{run_id}",
            ))
            summary["cancelled"] = "cleanup_confirmed"
        except BookingOutcomeUnknown:
            summary["cancelled"] = "cleanup_unknown"
            summary["manual_review_required"] = True
        except Exception:
            summary["cancelled"] = "cleanup_failed"
            summary["manual_review_required"] = True
    return SmokeResult(1, summary)


def _two_distinct_future_slots(slots: list[Slot], now: datetime) -> tuple[Slot, Slot]:
    future = sorted(
        (slot for slot in slots if slot.starts_at > now),
        key=lambda slot: (slot.starts_at, slot.staff_id, slot.id),
    )
    for index, first in enumerate(future):
        for second in future[index + 1:]:
            if second.id != first.id and second.starts_at != first.starts_at:
                return first, second
    raise _SmokeFailure("insufficient_distinct_future_slots")


def _require_booking(booking: ExternalBooking, customer_id: str, slot: Slot) -> None:
    if (
        booking.customer_id != customer_id
        or booking.slot_id != slot.id
        or booking.starts_at != slot.starts_at
        or booking.status != "confirmed"
    ):
        raise _SmokeFailure("record_mismatch")


def _owner_marker(customer_id: str) -> str:
    encoded = base64.urlsafe_b64encode(customer_id.encode()).decode().rstrip("=")
    return f"moroz:v1:{encoded}"


def _redacted_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _empty_summary() -> dict[str, object]:
    return {
        "success": False,
        "manual_review_required": False,
        "services_read": 0,
        "staff_read": 0,
        "slots_read": 0,
        "created": "not_started",
        "first_get": "not_started",
        "rescheduled": "not_started",
        "second_get": "not_started",
        "cancelled": "not_started",
        "final_state": "not_checked",
        "duplicate_marker_count": None,
        "record_id": None,
        "error": None,
    }


def main() -> int:
    try:
        settings = SandboxSmokeSettings.from_env(os.environ)
    except ValueError:
        result = SmokeResult(1, {**_empty_summary(), "error": "configuration_error"})
    else:
        result = asyncio.run(run_smoke(settings))
    print(json.dumps(result.summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
