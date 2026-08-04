import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from moroz.booking.models import BookingTemporaryError, Slot, SlotQuery
from moroz.booking.yclients_http import YclientsConfig
from moroz.booking.yclients_sandbox_smoke import YclientsSmokeBackend


@dataclass(frozen=True, slots=True)
class SandboxPreflightSettings:
    config: YclientsConfig = field(repr=False)
    service_id: str
    window_days: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "SandboxPreflightSettings":
        if env.get("YCLIENTS_ENVIRONMENT_LABEL", "") != "sandbox":
            raise ValueError("YCLIENTS_ENVIRONMENT_LABEL=sandbox is required")
        service_id = env.get("YCLIENTS_TEST_SERVICE_ID", "").strip()
        window = env.get("YCLIENTS_TEST_WINDOW_DAYS", "").strip()
        if not service_id.isdigit() or int(service_id) <= 0 or str(int(service_id)) != service_id:
            raise ValueError("YCLIENTS_TEST_SERVICE_ID must be a positive integer")
        if not window.isdigit() or not 1 <= int(window) <= 14:
            raise ValueError("YCLIENTS_TEST_WINDOW_DAYS must be an integer from 1 to 14")
        return cls(YclientsConfig.from_env(env), service_id, int(window))


@dataclass(frozen=True, slots=True)
class PreflightResult:
    exit_code: int
    summary: dict[str, object]


class PreflightBackend(Protocol):
    async def list_services(self, service_id: str) -> int: ...
    async def list_slots(self, query: SlotQuery) -> list[Slot]: ...
    async def reconcile_booking_key(
        self, booking_key: UUID, starts_at: datetime, ends_at: datetime
    ) -> dict[str, int]: ...


class _PreflightFailure(Exception):
    pass


async def run_preflight(
    settings: SandboxPreflightSettings,
    *,
    backend: PreflightBackend | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    uuid_factory: Callable[[], UUID] = uuid4,
) -> PreflightResult:
    actual = backend or YclientsSmokeBackend(settings.config)
    summary = _empty_summary()
    instant = now()
    try:
        summary["services_read"] = await actual.list_services(settings.service_id)
        slots = await actual.list_slots(SlotQuery(
            service_ids=(settings.service_id,),
            starts_after=instant,
            starts_before=instant + timedelta(days=settings.window_days),
        ))
        first, second = _two_distinct_future_slots(slots, instant)
        summary["slots_read"] = len(slots)
        summary["staff_read"] = len({slot.staff_id for slot in slots})
        records = await actual.reconcile_booking_key(
            uuid_factory(),
            min(first.starts_at, second.starts_at),
            max(first.starts_at, second.starts_at),
        )
        if not _is_empty_reconciliation(records):
            raise _PreflightFailure("record_read_preflight_mismatch")
        summary.update(records)
        summary["success"] = True
        return PreflightResult(0, summary)
    except _PreflightFailure as error:
        summary["error"] = str(error)
    except BookingTemporaryError:
        summary["error"] = "definite_provider_failure"
    except Exception:
        summary["error"] = "unexpected_failure"
    return PreflightResult(1, summary)


def _two_distinct_future_slots(slots: list[Slot], now: datetime) -> tuple[Slot, Slot]:
    future = sorted(
        (slot for slot in slots if slot.starts_at > now),
        key=lambda slot: (slot.starts_at, slot.staff_id, slot.id),
    )
    for index, first in enumerate(future):
        for second in future[index + 1:]:
            if second.id != first.id and second.starts_at != first.starts_at:
                return first, second
    raise _PreflightFailure("insufficient_distinct_future_slots")


def _is_empty_reconciliation(records: dict[str, int]) -> bool:
    return (
        set(records) == {"matches", "active_matches"}
        and type(records["matches"]) is int
        and type(records["active_matches"]) is int
        and records == {"matches": 0, "active_matches": 0}
    )


def _empty_summary() -> dict[str, object]:
    return {
        "success": False,
        "services_read": 0,
        "staff_read": 0,
        "slots_read": 0,
        "matches": 0,
        "active_matches": 0,
        "error": None,
    }


def main() -> int:
    try:
        settings = SandboxPreflightSettings.from_env(os.environ)
    except ValueError:
        result = PreflightResult(1, {**_empty_summary(), "error": "configuration_error"})
    else:
        result = asyncio.run(run_preflight(settings))
    print(json.dumps(result.summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
