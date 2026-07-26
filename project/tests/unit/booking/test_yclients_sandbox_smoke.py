import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from moroz.booking.models import (
    BookingNotFound,
    BookingOutcomeUnknown,
    BookingTemporaryError,
    ExternalBooking,
    Slot,
    SlotUnavailable,
)
from moroz.booking.yclients_http import HttpResponse, YclientsConfig
from moroz.booking.yclients_sandbox_smoke import (
    SandboxSmokeSettings,
    YclientsSmokeBackend,
    run_smoke,
)
from moroz.booking import yclients_sandbox_smoke


NOW = datetime(2026, 7, 22, 12, tzinfo=UTC)
RUN_ID = UUID("11111111-2222-4333-8444-555555555555")


def _env(**changes: str) -> dict[str, str]:
    values = {
        "YCLIENTS_PARTNER_TOKEN": "partner-sensitive",
        "YCLIENTS_USER_TOKEN": "user-sensitive",
        "YCLIENTS_COMPANY_ID": "123",
        "YCLIENTS_TEST_SERVICE_ID": "331",
        "YCLIENTS_TEST_NAME": "Synthetic Sensitive Name",
        "YCLIENTS_TEST_PHONE": "+70000000000",
        "YCLIENTS_SANDBOX_CONSENT": "yes",
    }
    values.update(changes)
    return values


def _slot(slot_id: str, hours: int) -> Slot:
    return Slot(slot_id, ("331",), "6544", NOW + timedelta(hours=hours), 60)


def _booking(slot: Slot, *, status: str = "confirmed") -> ExternalBooking:
    return ExternalBooking(
        "9001",
        f"smoke-{RUN_ID.hex}",
        RUN_ID,
        slot.id,
        slot.starts_at,
        status,
    )


class FakeBackend:
    def __init__(
        self,
        *,
        failure: tuple[str, Exception] | None = None,
        slots=None,
        created: ExternalBooking | None = None,
        final_status: str = "cancelled",
        reconciliation: dict[str, int] | None = None,
    ):
        self.calls: list[str] = []
        self.commands: list[object] = []
        self.failure = failure
        self.slots = slots or [_slot("slot-a", 48), _slot("slot-b", 72)]
        self.current = self.slots[0]
        self.created = created
        self.final_status = final_status
        self.reconciliation = reconciliation or {
            "matches": 1,
            "active_matches": 0,
        }

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
        assert query.service_ids == ("331",)
        assert query.starts_after == NOW + timedelta(days=1)
        assert query.starts_before == NOW + timedelta(days=14)
        return self.slots

    async def create_booking(self, command):
        self._call("create_booking")
        self.commands.append(command)
        assert command.customer_id == f"smoke-{RUN_ID.hex}"
        assert command.booking_key == RUN_ID
        assert command.customer_name == "Synthetic Sensitive Name"
        assert command.customer_phone == "+70000000000"
        assert command.personal_data_processing_allowed is True
        assert RUN_ID.hex in command.idempotency_key
        assert RUN_ID.hex in command.comment
        return self.created or _booking(self.slots[0])

    async def get_booking(self, command):
        name = "get_cancelled_booking" if "cancel_booking" in self.calls else "get_booking"
        self._call(name)
        self.commands.append(command)
        assert command.external_id == "9001"
        assert command.customer_id == f"smoke-{RUN_ID.hex}"
        assert command.booking_key == RUN_ID
        if name == "get_cancelled_booking":
            return _booking(self.current, status=self.final_status)
        return _booking(self.current)

    async def reschedule_booking(self, command):
        self._call("reschedule_booking")
        self.commands.append(command)
        assert command.external_id == "9001"
        assert command.customer_id == f"smoke-{RUN_ID.hex}"
        assert command.booking_key == RUN_ID
        self.current = self.slots[1]
        return _booking(self.current)

    async def cancel_booking(self, command):
        self._call("cancel_booking")
        self.commands.append(command)
        assert command.external_id == "9001"
        assert command.customer_id == f"smoke-{RUN_ID.hex}"
        assert command.booking_key == RUN_ID

    async def reconcile_booking_key(self, booking_key, starts_at, ends_at):
        self._call("reconcile_booking_key")
        assert booking_key == RUN_ID
        assert starts_at == self.slots[0].starts_at
        assert ends_at == self.slots[1].starts_at
        return self.reconciliation


@pytest.mark.parametrize(
    "missing",
    [
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_USER_TOKEN",
        "YCLIENTS_COMPANY_ID",
        "YCLIENTS_TEST_SERVICE_ID",
        "YCLIENTS_TEST_NAME",
        "YCLIENTS_TEST_PHONE",
    ],
)
def test_settings_require_every_live_value_by_name_only(missing: str) -> None:
    env = _env(**{missing: ""})

    with pytest.raises(ValueError, match=missing) as error:
        SandboxSmokeSettings.from_env(env)

    assert all(value not in str(error.value) for value in _env().values())


def test_settings_require_explicit_consent() -> None:
    with pytest.raises(ValueError, match="YCLIENTS_SANDBOX_CONSENT=yes"):
        SandboxSmokeSettings.from_env(_env(YCLIENTS_SANDBOX_CONSENT="no"))


def test_cli_readiness_failure_prints_one_fixed_redacted_summary(
    monkeypatch, capsys
) -> None:
    for name in _env():
        monkeypatch.delenv(name, raising=False)

    assert yclients_sandbox_smoke.main() == 1

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert json.loads(output)["error"] == "configuration_error"
    assert "YCLIENTS_" not in output


@pytest.mark.asyncio
async def test_successful_smoke_runs_the_exact_bounded_flow() -> None:
    backend = FakeBackend()

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 0
    assert backend.calls == [
        "list_services",
        "list_slots",
        "create_booking",
        "get_booking",
        "reschedule_booking",
        "get_booking",
        "cancel_booking",
        "get_cancelled_booking",
        "reconcile_booking_key",
    ]
    assert len(backend.commands) == 6
    assert all(command.customer_id == f"smoke-{RUN_ID.hex}" for command in backend.commands)
    assert all(command.booking_key == RUN_ID for command in backend.commands)
    assert result.summary == {
        "success": True,
        "manual_review_required": False,
        "services_read": 1,
        "staff_read": 1,
        "slots_read": 2,
        "created": "confirmed",
        "first_get": "confirmed",
        "rescheduled": "confirmed",
        "second_get": "confirmed",
        "cancelled": "confirmed",
        "final_state": "cancelled",
        "matches": 1,
        "active_matches": 0,
        "unknown_kind": None,
        "unknown_status": None,
        "error": None,
    }


@pytest.mark.asyncio
async def test_smoke_requires_two_distinct_future_instants_before_mutation() -> None:
    same_time = [_slot("slot-a", 48), _slot("slot-b", 48)]
    backend = FakeBackend(slots=same_time)

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls == ["list_services", "list_slots"]
    assert result.summary["error"] == "insufficient_distinct_future_slots"


@pytest.mark.asyncio
async def test_availability_failure_stops_before_all_mutations() -> None:
    backend = FakeBackend(failure=("list_slots", SlotUnavailable()))

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls == ["list_services", "list_slots"]
    assert backend.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("booking_key", [UUID(int=2), None])
async def test_invalid_create_result_never_triggers_cleanup_mutation(booking_key) -> None:
    backend = FakeBackend(created=ExternalBooking(
        "9001",
        f"smoke-{RUN_ID.hex}",
        booking_key,
        "slot-a",
        _slot("slot-a", 48).starts_at,
        "confirmed",
    ))

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls == ["list_services", "list_slots", "create_booking"]
    assert len(backend.commands) == 1


@pytest.mark.asyncio
async def test_unknown_outcome_aborts_without_blind_cleanup_and_redacts_output() -> None:
    backend = FakeBackend(
        failure=("reschedule_booking", BookingOutcomeUnknown(
            "forbidden detail", kind="http_status", status=503,
        ))
    )
    settings = SandboxSmokeSettings.from_env(_env())

    result = await run_smoke(
        settings, backend=backend, now=lambda: NOW, uuid_factory=lambda: RUN_ID
    )
    rendered = json.dumps(result.summary, sort_keys=True)

    assert result.exit_code == 1
    assert backend.calls == [
        "list_services",
        "list_slots",
        "create_booking",
        "get_booking",
        "reschedule_booking",
    ]
    assert result.summary["manual_review_required"] is True
    assert result.summary["error"] == "mutation_outcome_unknown"
    assert result.summary["unknown_kind"] == "http_status"
    assert result.summary["unknown_status"] == 503
    for value in _env().values():
        assert value not in rendered
    assert "forbidden detail" not in rendered
    assert RUN_ID.hex not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("transport", None),
        ("response_shape", 200),
        ("private-detail", 99),
        ("http_status", "500"),
        ("http_status", True),
        ("http_status", 600),
    ],
)
async def test_unknown_diagnostic_metadata_is_strictly_allowlisted(
    kind, status,
) -> None:
    backend = FakeBackend(failure=(
        "create_booking",
        BookingOutcomeUnknown("forbidden detail", kind=kind, status=status),
    ))

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )
    rendered = json.dumps(result.summary, sort_keys=True)

    assert result.summary["unknown_kind"] == (
        kind if kind in {"transport", "http_status", "response_shape"} else None
    )
    assert result.summary["unknown_status"] == (
        status if type(status) is int and 100 <= status <= 599 else None
    )
    assert "forbidden detail" not in rendered
    assert "private-detail" not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create_booking", "cancel_booking"])
async def test_each_mutation_unknown_stops_without_another_mutation(operation: str) -> None:
    backend = FakeBackend(failure=(operation, BookingOutcomeUnknown()))

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls[-1] == operation
    assert backend.calls.count(operation) == 1
    assert result.summary["manual_review_required"] is True


@pytest.mark.asyncio
async def test_definite_failure_after_create_attempts_one_cleanup_cancel() -> None:
    backend = FakeBackend(failure=("get_booking", BookingTemporaryError()))

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls == [
        "list_services",
        "list_slots",
        "create_booking",
        "get_booking",
        "cancel_booking",
    ]
    assert result.summary["cancelled"] == "cleanup_confirmed"
    assert result.summary["manual_review_required"] is False


@pytest.mark.asyncio
async def test_definite_primary_cancel_failure_is_not_retried_as_cleanup() -> None:
    backend = FakeBackend(failure=("cancel_booking", BookingTemporaryError()))

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert backend.calls[-1] == "cancel_booking"
    assert backend.calls.count("cancel_booking") == 1
    assert result.summary["cancelled"] == "failed"
    assert result.summary["manual_review_required"] is True


@pytest.mark.asyncio
async def test_final_not_found_is_accepted_as_deleted_evidence() -> None:
    backend = FakeBackend(failure=("get_cancelled_booking", BookingNotFound()))

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 0
    assert result.summary["final_state"] == "deleted"
    assert backend.calls[-1] == "reconcile_booking_key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("final_status", "reconciliation"),
    [
        ("confirmed", {"matches": 1, "active_matches": 0}),
        ("cancelled", {"matches": 0, "active_matches": 0}),
        ("cancelled", {"matches": 2, "active_matches": 0}),
        ("cancelled", {"matches": 1, "active_matches": 1}),
    ],
)
async def test_post_cancel_contradiction_requires_manual_review_without_new_mutation(
    final_status: str,
    reconciliation: dict[str, int],
) -> None:
    backend = FakeBackend(
        final_status=final_status,
        reconciliation=reconciliation,
    )

    result = await run_smoke(
        SandboxSmokeSettings.from_env(_env()),
        backend=backend,
        now=lambda: NOW,
        uuid_factory=lambda: RUN_ID,
    )

    assert result.exit_code == 1
    assert result.summary["manual_review_required"] is True
    assert backend.calls.count("create_booking") == 1
    assert backend.calls.count("reschedule_booking") == 1
    assert backend.calls.count("cancel_booking") == 1


class FakeHttp:
    def __init__(self, responses: list[HttpResponse]):
        self.responses = responses
        self.requests: list[tuple] = []

    async def request(self, method, path, *, query=(), json_body=None, user_auth=False):
        self.requests.append((method, path, tuple(query), user_auth))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_backend_reconciliation_counts_only_exact_canonical_booking_key() -> None:
    http = FakeHttp([
        HttpResponse(200, json.dumps({
            "success": True,
            "data": {"services": [{"id": 331}, {"id": 777}]},
        }).encode()),
        HttpResponse(200, json.dumps({
            "success": True,
            "data": [
                {
                    "id": 9001,
                    "api_id": str(RUN_ID),
                    "custom_fields": {"moroz_booking_key": str(RUN_ID)},
                    "deleted": True,
                },
                {
                    "id": 9002,
                    "api_id": str(RUN_ID),
                    "custom_fields": {"moroz_booking_key": str(UUID(int=2))},
                    "deleted": False,
                },
                {
                    "id": 9003,
                    "api_id": str(RUN_ID),
                    "custom_fields": {"foreign": str(RUN_ID)},
                    "deleted": False,
                },
                {
                    "id": 9004,
                    "api_id": "foreign",
                    "custom_fields": {"moroz_booking_key": str(UUID(int=3))},
                    "deleted": False,
                },
                {"id": 9005, "api_id": str(RUN_ID), "deleted": False},
            ],
        }).encode()),
    ])
    config = YclientsConfig.from_env(_env())
    backend = YclientsSmokeBackend(config, http=http)

    assert await backend.list_services("331") == 2
    assert await backend.reconcile_booking_key(RUN_ID, NOW, NOW + timedelta(days=2)) == {
        "matches": 1,
        "active_matches": 0,
    }
    assert http.requests == [
        ("GET", "/api/v1/book_services/123", (), False),
        (
            "GET",
            "/api/v1/records/123",
            (
                ("page", 1),
                ("count", 100),
                ("start_date", "2026-07-22"),
                ("end_date", "2026-07-24"),
                ("with_deleted", 1),
            ),
            True,
        ),
    ]


@pytest.mark.asyncio
async def test_backend_service_or_records_malformed_response_fails_closed() -> None:
    config = YclientsConfig.from_env(_env())
    backend = YclientsSmokeBackend(config, http=FakeHttp([
        HttpResponse(200, b'{"success":false}'),
    ]))

    with pytest.raises(BookingTemporaryError):
        await backend.list_services("331")


@pytest.mark.asyncio
async def test_reconciliation_rejects_non_mapping_custom_fields() -> None:
    backend = YclientsSmokeBackend(YclientsConfig.from_env(_env()), http=FakeHttp([
        HttpResponse(200, json.dumps({
            "success": True,
            "data": [{"custom_fields": [str(RUN_ID)]}],
        }).encode()),
    ]))

    with pytest.raises(BookingTemporaryError):
        await backend.reconcile_booking_key(RUN_ID, NOW, NOW + timedelta(days=1))


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [None, "false", 0])
async def test_reconciliation_requires_explicit_boolean_deleted_for_exact_key(deleted) -> None:
    record = {"custom_fields": {"moroz_booking_key": str(RUN_ID)}}
    if deleted is not None:
        record["deleted"] = deleted
    backend = YclientsSmokeBackend(YclientsConfig.from_env(_env()), http=FakeHttp([
        HttpResponse(200, json.dumps({"success": True, "data": [record]}).encode()),
    ]))

    with pytest.raises(BookingTemporaryError):
        await backend.reconcile_booking_key(RUN_ID, NOW, NOW + timedelta(days=1))


@pytest.mark.asyncio
async def test_duplicate_scan_paginates_until_a_short_page() -> None:
    http = FakeHttp([
        HttpResponse(200, json.dumps({
            "success": True,
            "data": [{"custom_fields": {"moroz_booking_key": str(RUN_ID)}, "deleted": False}] * 100,
        }).encode()),
        HttpResponse(200, json.dumps({
            "success": True,
            "data": [{"custom_fields": {"moroz_booking_key": str(RUN_ID)}, "deleted": False}],
        }).encode()),
    ])
    backend = YclientsSmokeBackend(YclientsConfig.from_env(_env()), http=http)

    assert await backend.reconcile_booking_key(RUN_ID, NOW, NOW + timedelta(days=1)) == {
        "matches": 101,
        "active_matches": 101,
    }
    assert [request[2][0] for request in http.requests] == [("page", 1), ("page", 2)]


@pytest.mark.asyncio
async def test_duplicate_scan_fails_closed_at_the_page_bound() -> None:
    page = HttpResponse(200, json.dumps({
        "success": True,
        "data": [{"custom_fields": {"foreign": "value"}}] * 100,
    }).encode())
    http = FakeHttp([page] * 20)
    backend = YclientsSmokeBackend(YclientsConfig.from_env(_env()), http=http)

    with pytest.raises(BookingTemporaryError):
        await backend.reconcile_booking_key(RUN_ID, NOW, NOW + timedelta(days=1))
    assert len(http.requests) == 20
