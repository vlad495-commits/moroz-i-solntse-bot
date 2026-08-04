import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from moroz.booking.models import BookingTemporaryError, Slot
from moroz.booking.yclients import YclientsAdapter
from moroz.booking.yclients_http import HttpResponse, YclientsConfig
from moroz.booking.yclients_sandbox_smoke import YclientsSmokeBackend
from moroz.booking.yclients_sandbox_preflight import (
    PreflightBackend,
    SandboxPreflightSettings,
    YclientsPreflightBackend,
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
        fields_read: int = 2,
    ) -> None:
        self.calls: list[str] = []
        self.failure = failure
        self.slots = slots or [_slot("slot-a", 48), _slot("slot-b", 72)]
        self.records = records or {"matches": 0, "active_matches": 0}
        self.fields_read = fields_read
        self.slot_query = None

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.failure and self.failure[0] == name:
            raise self.failure[1]

    async def list_services(self, service_id: str) -> int:
        self._call("list_services")
        assert service_id == "331"
        return 1

    async def list_record_custom_fields(self) -> int:
        self._call("record_fields")
        return self.fields_read

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


class FakeHttp:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, tuple[tuple[str, object], ...], bool]] = []

    async def request(self, method, path, *, query=(), user_auth=False, json_body=None):
        assert json_body is None
        self.requests.append((method, path, tuple(query), user_auth))
        return self.responses.pop(0)


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
async def test_concrete_preflight_backend_exposes_only_read_methods_and_uses_get_transport() -> None:
    http = FakeHttp([
        HttpResponse(200, b'{"success":true,"data":{"services":[{"id":331}]}}'),
        HttpResponse(200, b'{"success":true,"data":[]}'),
    ])
    backend = YclientsPreflightBackend(YclientsConfig.from_env(_env()), http=http)

    assert not {"create_booking", "reschedule_booking", "cancel_booking"} & set(
        YclientsPreflightBackend.__dict__
    )
    assert not isinstance(backend, YclientsSmokeBackend)
    assert not isinstance(backend._availability, YclientsAdapter)
    assert await backend.list_services("331") == 1
    assert await backend.reconcile_booking_key(RUN_ID, NOW, NOW + timedelta(days=1)) == {
        "matches": 0,
        "active_matches": 0,
    }
    assert [request[0] for request in http.requests] == ["GET", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_type", [YclientsPreflightBackend, YclientsSmokeBackend])
async def test_concrete_readiness_requires_both_hidden_editable_text_ownership_fields(
    backend_type,
) -> None:
    fields = {
        "success": True,
        "data": [
            {
                "custom_field": {
                    "code": "moroz_booking_key",
                    "type": {"code": "text"},
                    "user_can_edit": True,
                    "show_in_ui": False,
                }
            },
            {
                "custom_field": {
                    "code": "moroz_customer_id",
                    "type": {"code": "text"},
                    "user_can_edit": True,
                    "show_in_ui": False,
                }
            },
        ],
    }
    http = FakeHttp([HttpResponse(200, json.dumps(fields).encode())])
    backend = backend_type(YclientsConfig.from_env(_env()), http=http)

    assert await backend.list_record_custom_fields() == 2
    assert http.requests == [("GET", "/api/v1/custom_fields/record/123", (), True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_type", [YclientsPreflightBackend, YclientsSmokeBackend])
@pytest.mark.parametrize(
    "field_patch",
    [
        {"code": "other"},
        {"type": {"code": "number"}},
        {"user_can_edit": False},
        {"show_in_ui": True},
    ],
)
async def test_concrete_readiness_rejects_wrong_ownership_field_contract(
    backend_type, field_patch,
) -> None:
    field = {
        "code": "moroz_booking_key",
        "type": {"code": "text"},
        "user_can_edit": True,
        "show_in_ui": False,
    }
    field.update(field_patch)
    http = FakeHttp([HttpResponse(200, json.dumps({"success": True, "data": [{"custom_field": field}]}).encode())])
    backend = backend_type(YclientsConfig.from_env(_env()), http=http)

    with pytest.raises(BookingTemporaryError):
        await backend.list_record_custom_fields()


@pytest.mark.asyncio
async def test_concrete_preflight_reconciliation_ignores_empty_legacy_fields_list() -> None:
    http = FakeHttp([HttpResponse(200, json.dumps({
        "success": True,
        "data": [{"custom_fields": [], "deleted": True}],
    }).encode())])
    backend = YclientsPreflightBackend(YclientsConfig.from_env(_env()), http=http)

    assert await backend.reconcile_booking_key(
        RUN_ID, NOW, NOW + timedelta(days=1)
    ) == {"matches": 0, "active_matches": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_type", [YclientsPreflightBackend, YclientsSmokeBackend])
async def test_reconciliation_rejects_exact_key_without_expected_customer_marker(
    backend_type,
) -> None:
    http = FakeHttp([HttpResponse(200, json.dumps({
        "success": True,
        "data": [{
            "custom_fields": {"moroz_booking_key": str(RUN_ID)},
            "deleted": False,
        }],
    }).encode())])
    backend = backend_type(YclientsConfig.from_env(_env()), http=http)

    with pytest.raises(BookingTemporaryError):
        await backend.reconcile_booking_key(RUN_ID, NOW, NOW + timedelta(days=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_type", [YclientsPreflightBackend, YclientsSmokeBackend])
async def test_reconciliation_ignores_records_without_the_requested_booking_key(
    backend_type,
) -> None:
    http = FakeHttp([HttpResponse(200, json.dumps({
        "success": True,
        "data": [
            {},
            {"custom_fields": {}},
            {"custom_fields": {"moroz_customer_id": "foreign"}},
            {"custom_fields": []},
        ],
    }).encode())])
    backend = backend_type(YclientsConfig.from_env(_env()), http=http)

    assert await backend.reconcile_booking_key(
        RUN_ID, NOW, NOW + timedelta(days=1)
    ) == {"matches": 0, "active_matches": 0}


@pytest.mark.asyncio
async def test_preflight_sanitizes_default_backend_constructor_failure(monkeypatch) -> None:
    def failed_constructor(_config):
        raise RuntimeError("sensitive constructor failure")

    monkeypatch.setattr(yclients_sandbox_preflight, "YclientsPreflightBackend", failed_constructor)

    result = await run_preflight(SandboxPreflightSettings.from_env(_env()))

    assert result.exit_code == 1
    assert result.summary["error"] == "unexpected_failure"
    assert "sensitive" not in json.dumps(result.summary)


@pytest.mark.asyncio
async def test_preflight_sanitizes_now_failure() -> None:
    result = await run_preflight(
        SandboxPreflightSettings.from_env(_env()),
        backend=FakeReadBackend(),
        now=lambda: (_ for _ in ()).throw(RuntimeError("sensitive clock failure")),
    )

    assert result.exit_code == 1
    assert result.summary["error"] == "unexpected_failure"
    assert "sensitive" not in json.dumps(result.summary)


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
    assert backend.calls == ["record_fields", "list_services", "list_slots", "preflight_records"]
    assert backend.slot_query.starts_before == NOW + timedelta(days=14)
    assert result.summary["matches"] == 0
    assert result.summary["active_matches"] == 0
    assert result.summary["fields_read"] == 2


@pytest.mark.asyncio
async def test_preflight_stops_before_catalog_when_ownership_fields_are_missing() -> None:
    backend = FakeReadBackend(fields_read=1)

    result = await run_preflight(
        SandboxPreflightSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls == ["record_fields"]
    assert result.summary["error"] == "record_field_preflight_mismatch"


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
    assert backend.calls == ["record_fields", "list_services", "list_slots"]
    assert result.summary["error"] == "insufficient_distinct_future_slots"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_calls"),
    [
        (("preflight_records", BookingTemporaryError()), ["record_fields", "list_services", "list_slots", "preflight_records"]),
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
    assert backend.calls == ["record_fields", "list_services", "list_slots", "preflight_records"]
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


@pytest.mark.parametrize("stage", ["configuration", "runtime"])
def test_cli_sanitizes_any_expected_exception(monkeypatch, capsys, stage: str) -> None:
    if stage == "configuration":
        monkeypatch.setattr(
            yclients_sandbox_preflight.SandboxPreflightSettings,
            "from_env",
            lambda _env: (_ for _ in ()).throw(RuntimeError("sensitive config failure")),
        )
    else:
        async def failed_run(_settings):
            raise RuntimeError("sensitive runtime failure")

        monkeypatch.setattr(yclients_sandbox_preflight, "run_preflight", failed_run)

    assert yclients_sandbox_preflight.main() == 1

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert json.loads(output)["error"] in {"configuration_error", "unexpected_failure"}
    assert "sensitive" not in output
