import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from moroz.booking.models import BookingTemporaryError, Slot
from moroz.booking.yclients_sandbox_preflight import (
    PreflightBackend,
    SandboxPreflightSettings,
    run_preflight,
)
from moroz.booking import yclients_sandbox_preflight


NOW = datetime(2026, 7, 22, 12, tzinfo=UTC)
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")


def _env(**changes: str) -> dict[str, str]:
    values = {
        "YCLIENTS_PARTNER_TOKEN": "partner-sensitive",
        "YCLIENTS_USER_TOKEN": "user-sensitive",
        "YCLIENTS_COMPANY_ID": "123",
        "YCLIENTS_TEST_SERVICE_ID": "331",
        "YCLIENTS_ENVIRONMENT_LABEL": "sandbox",
        "YCLIENTS_TEST_WINDOW_DAYS": "14",
    }
    values.update(changes)
    return values


def _slot(slot_id: str, hours: int) -> Slot:
    return Slot(slot_id, ("331",), "6544", NOW + timedelta(hours=hours), 60)


class FakeReadBackend:
    def __init__(
        self,
        *,
        failure: tuple[str, Exception] | None = None,
        slots: list[Slot] | None = None,
        records: dict[str, int] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.failure = failure
        self.slots = slots or [_slot("slot-a", 48), _slot("slot-b", 72)]
        self.records = records or {"matches": 0, "active_matches": 0}
        self.slot_query = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.failure and self.failure[0] == name:
            raise self.failure[1]

    async def list_services(self, service_id: str) -> int:
        self._call("list_services")
        assert service_id == "331"
        return 1

    async def list_slots(self, query):
        self._call("list_slots")
        self.slot_query = query
        assert query.service_ids == ("331",)
        return self.slots

    async def reconcile_booking_key(self, booking_key, starts_at, ends_at) -> dict[str, int]:
        self._call("preflight_records")
        assert booking_key == RUN_ID
        assert starts_at == self.slots[0].starts_at
        assert ends_at == self.slots[1].starts_at
        return self.records


def test_settings_require_read_credentials_and_exact_sandbox_marker() -> None:
    settings = SandboxPreflightSettings.from_env(_env())

    assert settings.service_id == "331"
    assert settings.window_days == 14
    for patch in (
        {"YCLIENTS_PARTNER_TOKEN": ""},
        {"YCLIENTS_USER_TOKEN": ""},
        {"YCLIENTS_COMPANY_ID": ""},
        {"YCLIENTS_TEST_SERVICE_ID": ""},
        {"YCLIENTS_ENVIRONMENT_LABEL": "sandbox "},
        {"YCLIENTS_ENVIRONMENT_LABEL": "production"},
        {"YCLIENTS_TEST_WINDOW_DAYS": "0"},
        {"YCLIENTS_TEST_WINDOW_DAYS": "15"},
    ):
        with pytest.raises(ValueError):
            SandboxPreflightSettings.from_env(_env(**patch))


def test_preflight_backend_protocol_has_no_mutation_methods() -> None:
    forbidden = {"create_booking", "reschedule_booking", "cancel_booking"}

    assert not forbidden & set(PreflightBackend.__dict__)


@pytest.mark.asyncio
async def test_preflight_reads_services_slots_and_records_without_mutation() -> None:
    backend = FakeReadBackend()

    result = await run_preflight(
        SandboxPreflightSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 0
    assert backend.calls == ["list_services", "list_slots", "preflight_records"]
    assert backend.slot_query.starts_before == NOW + timedelta(days=14)
    assert result.summary["matches"] == 0
    assert result.summary["active_matches"] == 0


@pytest.mark.asyncio
async def test_preflight_requires_two_distinct_future_slots() -> None:
    backend = FakeReadBackend(slots=[_slot("slot-a", 48), _slot("slot-b", 48)])

    result = await run_preflight(
        SandboxPreflightSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls == ["list_services", "list_slots"]
    assert result.summary["error"] == "insufficient_distinct_future_slots"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_calls"),
    [
        (("preflight_records", BookingTemporaryError()), ["list_services", "list_slots", "preflight_records"]),
    ],
)
async def test_preflight_records_error_is_fail_closed(failure, expected_calls) -> None:
    backend = FakeReadBackend(failure=failure)

    result = await run_preflight(
        SandboxPreflightSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls == expected_calls
    assert result.summary["error"] == "definite_provider_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "records",
    [
        {"matches": 0},
        {"matches": "0", "active_matches": 0},
        {"matches": 1, "active_matches": 0},
        {"matches": 0, "active_matches": 1},
    ],
)
async def test_preflight_records_malformed_or_mismatched_is_fail_closed(records) -> None:
    backend = FakeReadBackend(records=records)

    result = await run_preflight(
        SandboxPreflightSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls == ["list_services", "list_slots", "preflight_records"]
    assert result.summary["error"] == "record_read_preflight_mismatch"


def test_cli_configuration_failure_prints_one_sanitized_json(monkeypatch, capsys) -> None:
    for name in _env():
        monkeypatch.delenv(name, raising=False)

    assert yclients_sandbox_preflight.main() == 1

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert json.loads(output)["error"] == "configuration_error"
    for value in _env().values():
        assert value not in output
    assert "YCLIENTS_" not in output
