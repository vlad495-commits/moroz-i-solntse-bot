import asyncio
import json
import os
import re
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
    GetBooking,
    RescheduleBooking,
    Slot,
    SlotQuery,
    SlotUnavailable,
)
from moroz.booking.yclients import YclientsAdapter
from moroz.booking.yclients_http import YclientsConfig, YclientsHttpClient, YclientsTransportError
from moroz.booking.yclients_sandbox_preflight import require_booking_permissions


_PAGE_SIZE = 100
_MAX_RECORD_PAGES = 20
_SANDBOX_CONSENT = "I_UNDERSTAND_THIS_CREATES_TEST_BOOKINGS"
_SANDBOX_LABEL = "sandbox"
_FAKE_PHONE = re.compile(r"\A\+7000[0-9]{7}\Z")
_FAKE_NAME_PREFIX = "Synthetic Test "
_OWNERSHIP_FIELD_CODES = {"moroz_booking_key", "moroz_customer_id"}


@dataclass(frozen=True, slots=True)
class SandboxSmokeSettings:
    config: YclientsConfig = field(repr=False)
    service_id: str
    window_days: int
    customer_name: str = field(repr=False)
    customer_phone: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SandboxSmokeSettings":
        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} is required")
            return value

        if env.get("YCLIENTS_SANDBOX_CONSENT", "") != _SANDBOX_CONSENT:
            raise ValueError(f"YCLIENTS_SANDBOX_CONSENT={_SANDBOX_CONSENT} is required")
        if env.get("YCLIENTS_ENVIRONMENT_LABEL", "") != _SANDBOX_LABEL:
            raise ValueError("YCLIENTS_ENVIRONMENT_LABEL=sandbox is required")
        service_id = required("YCLIENTS_TEST_SERVICE_ID")
        if not service_id.isdigit() or int(service_id) <= 0 or str(int(service_id)) != service_id:
            raise ValueError("YCLIENTS_TEST_SERVICE_ID must be a positive integer")
        window_text = required("YCLIENTS_TEST_WINDOW_DAYS")
        if not window_text.isdigit() or not 1 <= int(window_text) <= 14:
            raise ValueError("YCLIENTS_TEST_WINDOW_DAYS must be an integer from 1 to 14")
        customer_name = required("YCLIENTS_TEST_NAME")
        if not customer_name.startswith(_FAKE_NAME_PREFIX):
            raise ValueError("YCLIENTS_TEST_NAME must use the reserved synthetic test value")
        customer_phone = required("YCLIENTS_TEST_PHONE")
        if _FAKE_PHONE.fullmatch(customer_phone) is None:
            raise ValueError("YCLIENTS_TEST_PHONE must use the reserved +7000 fake prefix")
        return cls(
            config=YclientsConfig.from_env(env),
            service_id=service_id,
            window_days=int(window_text),
            customer_name=customer_name,
            customer_phone=customer_phone,
        )


@dataclass(frozen=True, slots=True)
class SmokeResult:
    exit_code: int
    summary: dict[str, object]


class SmokeBackend(Protocol):
    async def list_record_custom_fields(self) -> int: ...
    async def list_services(self, service_id: str) -> int: ...
    async def list_slots(self, query: SlotQuery) -> list[Slot]: ...
    async def create_booking(self, command: CreateBooking) -> ExternalBooking: ...
    async def get_booking(self, command: GetBooking) -> ExternalBooking: ...
    async def reschedule_booking(self, command: RescheduleBooking) -> ExternalBooking: ...
    async def cancel_booking(self, command: CancelBooking) -> None: ...
    async def reconcile_booking_key(
        self, booking_key: UUID, starts_at: datetime, ends_at: datetime
    ) -> dict[str, int]: ...


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

    async def list_record_custom_fields(self) -> int:
        data = await self._read(
            f"/api/v1/custom_fields/record/{self._config.company_id}",
            user_auth=True,
        )
        if not isinstance(data, list):
            raise BookingTemporaryError()
        matches: dict[str, Mapping[str, object]] = {}
        for wrapper in data:
            if not isinstance(wrapper, Mapping):
                raise BookingTemporaryError()
            field = wrapper.get("custom_field")
            if not isinstance(field, Mapping):
                raise BookingTemporaryError()
            code = field.get("code")
            if code not in _OWNERSHIP_FIELD_CODES:
                continue
            if not isinstance(code, str) or code in matches:
                raise BookingTemporaryError()
            matches[code] = field
        if set(matches) != _OWNERSHIP_FIELD_CODES:
            raise BookingTemporaryError()
        for field in matches.values():
            field_type = field.get("type")
            if (
                not isinstance(field_type, Mapping)
                or field_type.get("code") != "text"
                or field.get("user_can_edit") is not True
                or field.get("show_in_ui") is not False
            ):
                raise BookingTemporaryError()
        require_booking_permissions(await self._read(
            f"/api/v1/user/permissions/{self._config.company_id}",
            user_auth=True,
        ))
        return len(matches)

    async def list_slots(self, query: SlotQuery) -> list[Slot]:
        return await self._adapter.list_slots(query)

    async def create_booking(self, command: CreateBooking) -> ExternalBooking:
        return await self._adapter.create_booking(command)

    async def get_booking(self, command: GetBooking) -> ExternalBooking:
        return await self._adapter.get_booking(command)

    async def reschedule_booking(self, command: RescheduleBooking) -> ExternalBooking:
        return await self._adapter.reschedule_booking(command)

    async def cancel_booking(self, command: CancelBooking) -> None:
        await self._adapter.cancel_booking(command)

    async def reconcile_booking_key(
        self, booking_key: UUID, starts_at: datetime, ends_at: datetime
    ) -> dict[str, int]:
        matched = 0
        active_matched = 0
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
            for item in data:
                if "custom_fields" not in item:
                    continue
                fields = item["custom_fields"]
                if fields == []:
                    continue
                if not isinstance(fields, Mapping):
                    raise BookingTemporaryError()
                if fields.get("moroz_booking_key") != str(booking_key):
                    continue
                if fields.get("moroz_customer_id") != f"smoke-{booking_key.hex}":
                    raise BookingTemporaryError()
                deleted = item.get("deleted")
                if type(deleted) is not bool:
                    raise BookingTemporaryError()
                matched += 1
                if not deleted:
                    active_matched += 1
            if len(data) < _PAGE_SIZE:
                return {"matches": matched, "active_matches": active_matched}
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
    booking_key = uuid_factory()
    run_id = booking_key.hex
    customer_id = f"smoke-{run_id}"
    external_id: str | None = None
    cancel_confirmed = False
    cancel_attempted = False
    mutation_unknown = False
    mutation_started = False
    reconciliation_done = False
    reconciliation_bounds: tuple[datetime, datetime] | None = None
    instant = now()
    try:
        summary["fields_read"] = await actual.list_record_custom_fields()
        if summary["fields_read"] != len(_OWNERSHIP_FIELD_CODES):
            raise _SmokeFailure("record_field_preflight_mismatch")
        summary["services_read"] = await actual.list_services(settings.service_id)
        slots = await actual.list_slots(SlotQuery(
            service_ids=(settings.service_id,),
            starts_after=instant,
            starts_before=instant + timedelta(days=settings.window_days),
        ))
        first, second = _two_distinct_future_slots(slots, instant)
        reconciliation_bounds = (
            min(first.starts_at, second.starts_at),
            max(first.starts_at, second.starts_at),
        )
        summary["slots_read"] = len(slots)
        summary["staff_read"] = len({slot.staff_id for slot in slots})

        preflight = await actual.reconcile_booking_key(
            booking_key, *reconciliation_bounds
        )
        if preflight != {"matches": 0, "active_matches": 0}:
            raise _SmokeFailure("record_read_preflight_mismatch")

        mutation_started = True
        created = await actual.create_booking(CreateBooking(
            customer_id=customer_id,
            booking_key=booking_key,
            slot_id=first.id,
            idempotency_key=f"yclients-smoke-{run_id}",
            customer_name=settings.customer_name,
            customer_phone=settings.customer_phone,
            personal_data_processing_allowed=True,
            comment=f"moroz sandbox smoke {run_id}",
        ))
        _require_booking(created, customer_id, booking_key, first)
        external_id = created.external_id
        summary["created"] = "confirmed"

        fetched = await actual.get_booking(GetBooking(
            external_id, customer_id, booking_key,
        ))
        _require_booking(fetched, customer_id, booking_key, first)
        summary["first_get"] = "confirmed"

        mutation_started = True
        changed = await actual.reschedule_booking(RescheduleBooking(
            external_id=external_id,
            customer_id=customer_id,
            booking_key=booking_key,
            slot_id=second.id,
            idempotency_key=f"yclients-smoke-{run_id}",
        ))
        _require_booking(changed, customer_id, booking_key, second)
        summary["rescheduled"] = "confirmed"

        fetched = await actual.get_booking(GetBooking(
            external_id, customer_id, booking_key,
        ))
        _require_booking(fetched, customer_id, booking_key, second)
        summary["second_get"] = "confirmed"

        mutation_started = True
        cancel_attempted = True
        await actual.cancel_booking(CancelBooking(
            external_id=external_id,
            customer_id=customer_id,
            booking_key=booking_key,
            idempotency_key=f"yclients-smoke-{run_id}",
        ))
        cancel_confirmed = True
        summary["cancelled"] = "confirmed"
        try:
            cancelled = await actual.get_booking(GetBooking(
                external_id, customer_id, booking_key,
            ))
        except BookingNotFound:
            summary["final_state"] = "deleted"
        else:
            if cancelled.customer_id != customer_id or cancelled.status != "cancelled":
                raise _SmokeFailure("cancelled_record_not_confirmed")
            summary["final_state"] = "cancelled"

        reconciliation_done = True
        reconciliation = await actual.reconcile_booking_key(booking_key, *reconciliation_bounds)
        summary.update(reconciliation)
        if reconciliation != {"matches": 1, "active_matches": 0}:
            raise _SmokeFailure("reconciliation_mismatch")
        summary["success"] = True
        return SmokeResult(0, summary)
    except BookingOutcomeUnknown as error:
        mutation_unknown = mutation_started
        summary["manual_review_required"] = True
        summary["error"] = "mutation_outcome_unknown"
        _record_unknown_metadata(summary, error)
    except _SmokeFailure as error:
        summary["error"] = str(error)
        if cancel_confirmed:
            summary["manual_review_required"] = True
    except (BookingNotFound, BookingTemporaryError, SlotUnavailable):
        summary["error"] = "definite_provider_failure"
        if reconciliation_done:
            summary["manual_review_required"] = True
    except Exception:
        summary["error"] = "unexpected_failure"

    if cancel_attempted and not cancel_confirmed:
        summary["cancelled"] = "failed"
        summary["manual_review_required"] = True
    if (
        external_id is not None
        and not cancel_confirmed
        and not cancel_attempted
        and not mutation_unknown
    ):
        try:
            mutation_started = True
            cancel_attempted = True
            await actual.cancel_booking(CancelBooking(
                external_id=external_id,
                customer_id=customer_id,
                booking_key=booking_key,
                idempotency_key=f"yclients-smoke-cleanup-{run_id}",
            ))
            summary["cancelled"] = "cleanup_confirmed"
        except BookingOutcomeUnknown as error:
            mutation_unknown = True
            summary["cancelled"] = "cleanup_unknown"
            summary["manual_review_required"] = True
            _record_unknown_metadata(summary, error)
        except Exception:
            summary["cancelled"] = "cleanup_failed"
            summary["manual_review_required"] = True
    if reconciliation_bounds is not None and not reconciliation_done and (
        mutation_unknown or external_id is not None
    ):
        reconciliation_done = True
        try:
            reconciliation = await actual.reconcile_booking_key(
                booking_key, *reconciliation_bounds
            )
        except Exception:
            summary["manual_review_required"] = True
        else:
            summary.update(reconciliation)
            if reconciliation != {"matches": 1, "active_matches": 0}:
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


def _require_booking(
    booking: ExternalBooking,
    customer_id: str,
    booking_key: UUID,
    slot: Slot,
) -> None:
    if (
        booking.customer_id != customer_id
        or booking.booking_key != booking_key
        or booking.slot_id != slot.id
        or booking.starts_at != slot.starts_at
        or booking.status != "confirmed"
    ):
        raise _SmokeFailure("record_mismatch")


def _record_unknown_metadata(
    summary: dict[str, object], error: BookingOutcomeUnknown,
) -> None:
    if error.kind in {"transport", "http_status", "response_shape"}:
        summary["unknown_kind"] = error.kind
    if type(error.status) is int and 100 <= error.status <= 599:
        summary["unknown_status"] = error.status


def _empty_summary() -> dict[str, object]:
    return {
        "success": False,
        "manual_review_required": False,
        "fields_read": 0,
        "services_read": 0,
        "staff_read": 0,
        "slots_read": 0,
        "created": "not_started",
        "first_get": "not_started",
        "rescheduled": "not_started",
        "second_get": "not_started",
        "cancelled": "not_started",
        "final_state": "not_checked",
        "matches": 0,
        "active_matches": 0,
        "unknown_kind": None,
        "unknown_status": None,
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
