import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from moroz.booking.catalog import CatalogService, CatalogStaff
from moroz.booking.models import BookingTemporaryError
from moroz.booking.yclients import YclientsAvailabilityAdapter
from moroz.booking.yclients_catalog import YclientsCatalogAdapter
from moroz.booking.yclients_http import HttpResponse, YclientsConfig
from moroz.booking.yclients_readonly_check import (
    ReadonlyCheckError,
    ReadonlySettings,
    main,
    run_readonly_check,
)
from moroz.booking import yclients_readonly_check


NOW = datetime(2026, 8, 2, 9, 0, tzinfo=UTC)


class FakeHttp:
    def __init__(self, payloads: list[object]) -> None:
        self.payloads = list(payloads)
        self.methods: list[str] = []
        self.paths: list[str] = []

    async def request(self, method, path, *, query=(), json_body=None, user_auth=False):
        assert json_body is None
        assert user_auth is False
        self.methods.append(method)
        self.paths.append(path)
        payload = self.payloads.pop(0)
        return HttpResponse(
            200,
            json.dumps(
                {"success": True, "data": payload}, ensure_ascii=False
            ).encode(),
        )


def _real_readers(http: FakeHttp):
    config = YclientsConfig(
        base_url="https://provider.invalid",
        partner_token="synthetic-partner",
        user_token="synthetic-readonly-slot-key",
        company_id=42,
    )
    return (
        YclientsCatalogAdapter(http, "42", ("1",), ("7",)),
        YclientsAvailabilityAdapter(config, http=http),
    )


@pytest.mark.asyncio
async def test_readonly_check_calls_only_get_and_returns_sanitized_counts():
    http = FakeHttp(
        [
            {"services": [{"id": 1, "title": "Private title", "duration": 1800}]},
            [{"id": 7, "name": "Private name", "bookable": True}],
            [],
            [{"id": 7, "name": "Private name", "bookable": True}],
        ]
    )
    catalog, availability = _real_readers(http)

    result = await run_readonly_check(
        catalog,
        availability,
        service_ids=("1",),
        staff_ids=("7",),
        environment_label="local-fake",
        now=NOW,
        horizon_days=14,
    )

    rendered = json.dumps(result.summary, sort_keys=True)
    assert result.ok is True
    assert set(http.methods) == {"GET"}
    for forbidden in ("create_booking", "reschedule_booking", "cancel_booking"):
        assert not hasattr(availability, forbidden)
    assert result.summary == {
        "environment": "local-fake",
        "horizon_days": 14,
        "service_ids": ["1"],
        "staff_ids": ["7"],
        "service_count": 1,
        "staff_count": 1,
        "availability_counts": {"7": 0},
        "availability_total": 0,
    }
    for private in ("private title", "private name", "phone", "token", "url"):
        assert private not in rendered.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "availability_staff",
    [
        [],
        [
            {"id": 7, "bookable": True},
            {"id": 7, "bookable": True},
        ],
    ],
)
async def test_readonly_check_rejects_partial_availability_staff_response(
    availability_staff,
):
    http = FakeHttp(
        [
            {"services": [{"id": 1, "title": "Service", "duration": 1800}]},
            [{"id": 7, "name": "Staff", "bookable": True}],
            [],
            availability_staff,
        ]
    )
    catalog, availability = _real_readers(http)

    with pytest.raises(ReadonlyCheckError):
        await run_readonly_check(
            catalog,
            availability,
            service_ids=("1",),
            staff_ids=("7",),
            environment_label="local-fake",
            now=NOW,
        )

    assert http.methods == ["GET"] * 4
    assert not any("/book_times/" in path for path in http.paths)


class FakeCatalog:
    def __init__(self, services, staff):
        self.services = services
        self.staff = staff

    async def list_services(self):
        if isinstance(self.services, Exception):
            raise self.services
        return self.services

    async def list_staff(self, service_ids):
        assert service_ids == ("1",)
        if isinstance(self.staff, Exception):
            raise self.staff
        return self.staff


class FakeAvailability:
    def __init__(self):
        self.queries = []

    async def list_slots(self, query):
        self.queries.append(query)
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("services", "staff"),
    [
        ([], [CatalogStaff("7", "Staff", ("1",))]),
        (
            [CatalogService("1", "Service", 30)] * 2,
            [CatalogStaff("7", "Staff", ("1",))],
        ),
        ([CatalogService("1", "Service", 30)], []),
        (
            [CatalogService("1", "Service", 30)],
            [CatalogStaff("7", "Staff", ("1",))] * 2,
        ),
    ],
)
async def test_readonly_check_rejects_missing_or_duplicate_allowlisted_items(
    services, staff
):
    with pytest.raises(ReadonlyCheckError) as raised:
        await run_readonly_check(
            FakeCatalog(services, staff),
            FakeAvailability(),
            service_ids=("1",),
            staff_ids=("7",),
            environment_label="local-fake",
            now=NOW,
        )

    assert str(raised.value) == "read-only preflight failed"


@pytest.mark.asyncio
async def test_readonly_check_uses_exact_bounded_window_for_each_staff():
    availability = FakeAvailability()
    await run_readonly_check(
        FakeCatalog(
            [CatalogService("1", "Service", 30)],
            [CatalogStaff("7", "Staff", ("1",))],
        ),
        availability,
        service_ids=("1",),
        staff_ids=("7",),
        environment_label="local-fake",
        now=NOW,
    )

    assert len(availability.queries) == 1
    query = availability.queries[0]
    assert query.service_ids == ("1",)
    assert query.staff_id == "7"
    assert query.starts_after == NOW
    assert query.starts_before.isoformat() == "2026-08-16T09:00:00+00:00"


@pytest.mark.asyncio
async def test_readonly_check_fails_closed_without_leaking_provider_error():
    with pytest.raises(ReadonlyCheckError) as raised:
        await run_readonly_check(
            FakeCatalog(BookingTemporaryError("private payload"), []),
            FakeAvailability(),
            service_ids=("1",),
            staff_ids=("7",),
            environment_label="local-fake",
            now=NOW,
        )

    assert "private" not in str(raised.value)


def test_readonly_module_has_no_mutation_method_references():
    source = Path(
        "/workspace/src/moroz/booking/yclients_readonly_check.py"
    ).read_text(encoding="utf-8")

    for forbidden in ("create_booking", "reschedule_booking", "cancel_booking"):
        assert forbidden not in source


def test_readonly_availability_transport_boundary_hardcodes_get():
    assert tuple(inspect.signature(YclientsAvailabilityAdapter._read).parameters) == (
        "self",
        "path",
        "query",
    )


def test_readonly_settings_need_no_provider_user_token():
    settings = ReadonlySettings.from_env(
        {
            "YCLIENTS_PARTNER_TOKEN": "synthetic-partner",
            "YCLIENTS_USER_TOKEN": "must-not-be-consumed",
            "YCLIENTS_COMPANY_ID": "42",
            "YCLIENTS_SERVICE_ALLOWLIST": "1",
            "YCLIENTS_STAFF_ALLOWLIST": "7",
            "YCLIENTS_ENVIRONMENT_LABEL": "local-fake",
        }
    )

    assert settings.config.user_token != "must-not-be-consumed"
    assert settings.service_ids == ("1",)
    assert settings.staff_ids == ("7",)


def test_cli_failure_output_is_bounded_and_sanitized(monkeypatch, capsys):
    async def fail(_env):
        raise RuntimeError("private token phone url provider payload")

    monkeypatch.setattr(yclients_readonly_check, "_run_from_env", fail)

    assert main() == 1
    assert capsys.readouterr().out.strip() == '{"ok":false}'


def test_compose_readonly_profile_has_only_required_provider_settings():
    compose = Path("/workspace/docker-compose.yml").read_text(encoding="utf-8")
    service = compose.split("\n  yclients-readonly:\n", 1)[1].split(
        "\n  yclients-smoke:\n", 1
    )[0]

    assert 'profiles: ["yclients-readonly"]' in service
    command = '["python", "-m", "moroz.booking.yclients_readonly_check"]'
    assert f"command: {command}" in service
    for required in (
        "YCLIENTS_PARTNER_TOKEN",
        "YCLIENTS_COMPANY_ID",
        "YCLIENTS_SERVICE_ALLOWLIST",
        "YCLIENTS_STAFF_ALLOWLIST",
        "YCLIENTS_ENVIRONMENT_LABEL",
    ):
        assert required in service
    for forbidden in (
        "YCLIENTS_USER_TOKEN",
        "TELEGRAM",
        "LLM_",
        "OPENAI",
        "DATABASE",
        "POSTGRES",
        "RABBITMQ",
        "REDIS",
    ):
        assert forbidden not in service
