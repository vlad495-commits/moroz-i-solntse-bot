import importlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
booking_routes = importlib.import_module("booking_routes")
admin_app = importlib.import_module("app")


def _user(role="owner"):
    return auth.AuthenticatedUser(
        id=7,
        username=role,
        role=role,
        csrf_token="known-csrf",
        session_id="session-id",
    )


def _app():
    app = FastAPI()
    app.include_router(booking_routes.router)
    return app


def _page(booking_id=None):
    booking_id = booking_id or uuid4()
    return {
        "items": [{
            "id": booking_id,
            "detail_id": booking_id,
            "external_id": "9001",
            "row_key": "y:9001",
            "customer_chat_id": 42,
            "customer_label": "Клиент #42",
            "starts_at": datetime(2026, 8, 26, 7, 0, tzinfo=UTC),
            "scheduled_end_at": datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
            "status": "confirmed",
            "status_label": "Подтверждена",
            "updated_at": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
            "scenario_label": "Создание записи",
            "phase_label": "Подтверждено",
            "error_label": None,
            "source": "bot",
            "source_label": "Создано ботом",
            "reconciliation_state": "in_sync",
            "reconciliation_label": "Синхронизировано",
            "client_name": "<client>",
            "staff_name": "<staff>",
            "service_names": ["<service>"],
            "private_phone": "+78881234567",
        }],
        "freshness": {
            "last_success_at": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
            "stale": False,
        },
    }


def _detail(booking_id):
    return {
        **_page()["items"][0],
        "id": booking_id,
        "external_id": "<provider-id>",
        "events": [{
            "id": uuid4(),
            "created_at": datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
            "title": "Запись подтверждена",
        }],
    }


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    async def service_options(_database):
        return [{
            "service_id": "331",
            "staff_id": "6544",
            "service_name": "Криотерапия",
            "staff_name": "Анна",
            "duration_minutes": 60,
        }]

    monkeypatch.setattr(booking_routes, "list_booking_service_options", service_options)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_staff_can_read_safe_calendar_and_detail(monkeypatch, role):
    booking_id = uuid4()
    calls = []

    async def current_user(_request):
        return _user(role)

    async def calendar(database, **kwargs):
        calls.append((database, kwargs))
        return _page(booking_id)

    async def detail(database, value, **kwargs):
        calls.append((database, value, kwargs))
        return _detail(value)

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_calendar_bookings", calendar)
    monkeypatch.setattr(booking_routes, "get_booking_detail", detail)
    async with AsyncClient(
        transport=ASGITransport(app=_app(), root_path="/admin"),
        base_url="http://test/admin",
    ) as client:
        page = await client.get("/bookings/?week=2026-08-24")
        detail_response = await client.get(f"/bookings/{booking_id}")

    assert page.status_code == detail_response.status_code == 200
    assert "/admin/bookings/?week=2026-08-17" in page.text
    assert "/admin/bookings/?week=2026-08-31" in page.text
    assert f"/admin/bookings/{booking_id}" in page.text
    assert "/admin/bookings/external/9001/status" in page.text
    assert "&lt;client&gt;" in page.text and "<client>" not in page.text
    assert "&lt;service&gt;" in page.text
    assert "<staff>" not in page.text
    assert "+78881234567" not in page.text
    assert "&lt;provider-id&gt;" in detail_response.text
    assert calls[0][0] == "database"
    assert (calls[0][1]["week_end"] - calls[0][1]["week_start"]).days == 7
    assert calls[1][2]["actor_id"] == 7


@pytest.mark.asyncio
async def test_yclients_only_card_is_safe_and_actionable(monkeypatch):
    async def current_user(_request):
        return _user()

    async def calendar(_database, **_kwargs):
        page = _page()
        page["items"][0].update(
            id=None,
            detail_id=None,
            source="other",
            client_name="<yclients-client>",
        )
        return page

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_calendar_bookings", calendar)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/bookings/?week=2026-08-24")

    assert response.status_code == 200
    assert "&lt;yclients-client&gt;" in response.text
    assert "/bookings/external/9001/status" in response.text
    assert "/bookings/None" not in response.text


@pytest.mark.asyncio
async def test_projection_warnings_are_safe(monkeypatch):
    async def current_user(_request):
        return _user()

    async def calendar(_database, **_kwargs):
        page = _page()
        page["freshness"] = {
            "last_success_at": datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
            "stale": True,
            "last_failure_at": datetime(2026, 8, 25, 9, 30, tzinfo=UTC),
            "last_failure_label": "Сервис сверки временно недоступен",
            "last_error_code": "private-provider-body",
        }
        return page

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_calendar_bookings", calendar)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/bookings/?week=2026-08-24")

    assert response.status_code == 200
    assert "Данные YCLIENTS могут быть устаревшими" in response.text
    assert "Сервис сверки временно недоступен" in response.text
    assert "private-provider-body" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "viewer"])
async def test_non_staff_is_rejected_before_database(monkeypatch, role):
    async def current_user(_request):
        return _user(role)

    def forbidden_database():
        raise AssertionError("database must not be accessed before RBAC")

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", forbidden_database)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        get_response = await client.get("/bookings/")
        post_response = await client.post(
            "/bookings/manual", data={"csrf_token": "known-csrf"}
        )

    assert get_response.status_code == post_response.status_code == 403


@pytest.mark.asyncio
async def test_anonymous_booking_read_redirects_to_login(monkeypatch):
    async def login_required(_request):
        raise auth._LoginRequired

    monkeypatch.setattr(booking_routes, "get_current_user", login_required)
    async with AsyncClient(
        transport=ASGITransport(app=admin_app.app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/bookings/")

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_invalid_week_and_read_failure_are_safe(monkeypatch):
    async def current_user(_request):
        return _user()

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("database secret must not be returned")

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_calendar_bookings", unavailable)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        invalid = await client.get("/bookings/?week=bad")
        unavailable_response = await client.get("/bookings/?week=2026-08-24")

    assert invalid.status_code == 422
    assert unavailable_response.status_code == 503
    assert "database secret" not in unavailable_response.text


@pytest.mark.asyncio
async def test_manual_booking_checks_csrf_catalog_and_enqueues(monkeypatch):
    queued = []

    async def current_user(_request):
        return _user()

    async def enqueue(database, **kwargs):
        queued.append((database, kwargs))

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "enqueue_admin_booking_command", enqueue)
    data = {
        "customer_name": "Анна",
        "customer_phone": "+79990000000",
        "service_staff": "331:6544",
        "starts_at": "2026-09-02T10:00",
        "consent": "yes",
        "csrf_token": "known-csrf",
    }
    async with AsyncClient(
        transport=ASGITransport(app=_app(), root_path="/admin"),
        base_url="http://test/admin",
        follow_redirects=False,
    ) as client:
        bad_csrf = await client.post(
            "/bookings/manual", data={**data, "csrf_token": "bad"}
        )
        accepted = await client.post("/bookings/manual", data=data)

    assert bad_csrf.status_code == 403
    assert accepted.status_code == 303
    assert accepted.headers["location"].startswith(
        "/admin/bookings/?week=2026-09-02&notice=queued"
    )
    assert queued[0][0] == "database"
    assert queued[0][1]["payload"]["service_id"] == "331"


@pytest.mark.asyncio
async def test_status_action_checks_csrf_and_allowlist(monkeypatch):
    queued = []

    async def current_user(_request):
        return _user()

    async def enqueue(_database, **kwargs):
        queued.append(kwargs)

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "enqueue_admin_booking_command", enqueue)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        bad_csrf = await client.post(
            "/bookings/external/9001/status",
            data={"status": "completed", "csrf_token": "bad"},
        )
        bad_status = await client.post(
            "/bookings/external/9001/status",
            data={"status": "private", "csrf_token": "known-csrf"},
        )
        accepted = await client.post(
            "/bookings/external/9001/status",
            data={"status": "completed", "csrf_token": "known-csrf"},
        )

    assert bad_csrf.status_code == 403
    assert bad_status.status_code == 422
    assert accepted.status_code == 303
    assert len(queued) == 1
    assert queued[0]["kind"] == "admin_booking_status"
    assert queued[0]["payload"] == {
        "external_id": "9001",
        "status": "completed",
    }
    assert queued[0]["actor_id"] == 7


def test_booking_routes_do_not_depend_on_live_provider_clients():
    source = open(booking_routes.__file__, encoding="utf-8").read()

    assert "YclientsAdapter" not in source
    assert "YCLIENTS_PARTNER_TOKEN" not in source
