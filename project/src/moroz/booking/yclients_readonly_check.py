import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from moroz.booking.catalog import (
    BookingCatalogPort,
    parse_id_allowlist,
)
from moroz.booking.models import Slot, SlotQuery
from moroz.booking.yclients import YclientsAvailabilityAdapter
from moroz.booking.yclients_catalog import YclientsCatalogAdapter
from moroz.booking.yclients_http import YclientsConfig, YclientsHttpClient


HORIZON_DAYS = 14
_SAFE_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_LOCAL_SLOT_KEY = "readonly-local-slot-key-not-a-provider-secret"


class ReadonlyAvailabilityPort(Protocol):
    async def list_slots(self, query: SlotQuery) -> list[Slot]: ...


class ReadonlyCheckError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("read-only preflight failed")


@dataclass(frozen=True, slots=True)
class ReadonlyCheckResult:
    ok: bool
    summary: dict[str, object]


@dataclass(frozen=True, slots=True)
class ReadonlySettings:
    config: YclientsConfig
    service_ids: tuple[str, ...]
    staff_ids: tuple[str, ...]
    environment_label: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ReadonlySettings":
        label = _environment_label(env.get("YCLIENTS_ENVIRONMENT_LABEL", ""))
        service_ids = parse_id_allowlist(
            env.get("YCLIENTS_SERVICE_ALLOWLIST", ""), "services"
        )
        staff_ids = parse_id_allowlist(
            env.get("YCLIENTS_STAFF_ALLOWLIST", ""), "staff"
        )
        readonly_env = {
            name: env.get(name, "")
            for name in (
                "YCLIENTS_PARTNER_TOKEN",
                "YCLIENTS_COMPANY_ID",
                "YCLIENTS_BASE_URL",
                "YCLIENTS_TIMEZONE",
                "YCLIENTS_TIMEOUT_SECONDS",
            )
        }
        readonly_env["YCLIENTS_USER_TOKEN"] = _LOCAL_SLOT_KEY
        return cls(
            config=YclientsConfig.from_env(readonly_env),
            service_ids=service_ids,
            staff_ids=staff_ids,
            environment_label=label,
        )


async def run_readonly_check(
    catalog: BookingCatalogPort,
    availability: ReadonlyAvailabilityPort,
    *,
    service_ids: tuple[str, ...],
    staff_ids: tuple[str, ...],
    environment_label: str,
    now: datetime,
    horizon_days: int = HORIZON_DAYS,
) -> ReadonlyCheckResult:
    try:
        _require_inputs(
            service_ids,
            staff_ids,
            environment_label,
            now,
            horizon_days,
        )
        services = await catalog.list_services()
        _require_exact_ids(
            tuple(service.id for service in services), service_ids
        )
        staff = await catalog.list_staff(service_ids)
        _require_exact_ids(tuple(member.id for member in staff), staff_ids)
        expected_services = frozenset(service_ids)
        if any(
            len(member.service_ids) != len(expected_services)
            or frozenset(member.service_ids) != expected_services
            for member in staff
        ):
            raise ReadonlyCheckError()

        end = now + timedelta(days=horizon_days)
        availability_counts: dict[str, int] = {}
        seen_slot_ids: set[str] = set()
        for staff_id in staff_ids:
            slots = await availability.list_slots(
                SlotQuery(service_ids, now, end, staff_id)
            )
            for slot in slots:
                if (
                    slot.staff_id != staff_id
                    or len(slot.service_ids) != len(expected_services)
                    or frozenset(slot.service_ids) != expected_services
                    or slot.starts_at < now
                    or slot.starts_at >= end
                    or slot.duration_minutes <= 0
                    or not slot.id
                    or slot.id in seen_slot_ids
                ):
                    raise ReadonlyCheckError()
                seen_slot_ids.add(slot.id)
            availability_counts[staff_id] = len(slots)

        summary: dict[str, object] = {
            "environment": environment_label,
            "horizon_days": horizon_days,
            "service_ids": list(service_ids),
            "staff_ids": list(staff_ids),
            "service_count": len(services),
            "staff_count": len(staff),
            "availability_counts": availability_counts,
            "availability_total": sum(availability_counts.values()),
        }
        return ReadonlyCheckResult(ok=True, summary=summary)
    except ReadonlyCheckError:
        raise
    except Exception:
        raise ReadonlyCheckError() from None


def _require_inputs(
    service_ids: tuple[str, ...],
    staff_ids: tuple[str, ...],
    environment_label: str,
    now: datetime,
    horizon_days: int,
) -> None:
    parse_id_allowlist(",".join(service_ids), "services")
    parse_id_allowlist(",".join(staff_ids), "staff")
    _environment_label(environment_label)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ReadonlyCheckError()
    if horizon_days != HORIZON_DAYS:
        raise ReadonlyCheckError()


def _require_exact_ids(actual: tuple[str, ...], expected: tuple[str, ...]) -> None:
    if len(actual) != len(expected) or set(actual) != set(expected):
        raise ReadonlyCheckError()


def _environment_label(value: str) -> str:
    label = value.strip()
    if not _SAFE_LABEL.fullmatch(label):
        raise ReadonlyCheckError()
    return label


async def _run_from_env(env: Mapping[str, str]) -> ReadonlyCheckResult:
    settings = ReadonlySettings.from_env(env)
    http = YclientsHttpClient(settings.config)
    return await run_readonly_check(
        YclientsCatalogAdapter(
            http,
            str(settings.config.company_id),
            settings.service_ids,
            settings.staff_ids,
        ),
        YclientsAvailabilityAdapter(settings.config, http=http),
        service_ids=settings.service_ids,
        staff_ids=settings.staff_ids,
        environment_label=settings.environment_label,
        now=datetime.now(UTC),
    )


def main() -> int:
    try:
        result = asyncio.run(_run_from_env(os.environ))
    except Exception:
        print(json.dumps({"ok": False}, separators=(",", ":")))
        return 1
    print(
        json.dumps(
            {"ok": result.ok, **result.summary},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
