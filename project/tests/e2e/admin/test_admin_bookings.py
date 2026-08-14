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
        "items": [
            {
                "id": booking_id,
                "customer_id": "42",
                "starts_at": datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
                "scheduled_end_at": None,
                "status": "confirmed",
                "status_label": "Подтверждена",
                "updated_at": datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
                "scenario_label": "Создание записи",
                "phase_label": "Подтверждено",
                "error_label": None,
                "snapshot": "private snapshot",
                "state": "private state",
                "payload": "private payload",
                "status_raw": "private enum",
            }
        ],
        "has_more": True,
        "next_cursor": "next-cursor",
    }


def _detail(booking_id):
    return {
        **_page()["items"][0],
        "id": booking_id,
        "external_id": "<provider-id>",
        "events": [
            {
                "id": uuid4(),
                "created_at": datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
                "title": "Запись подтверждена",
            }
        ],
    }


@pytest.mark.asyncio
async def test_staff_can_read_safe_booking_list_and_detail(monkeypatch):
    booking_id = uuid4()
    calls = []

    async def current_user(_request):
        return _user("admin")

    async def list_projection(database, **kwargs):
        calls.append((database, kwargs))
        return _page(booking_id)

    async def detail_projection(database, value, **kwargs):
        calls.append((database, value, kwargs))
        return _detail(value)

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_bookings", list_projection)
    monkeypatch.setattr(booking_routes, "get_booking_detail", detail_projection)
    async with AsyncClient(
        transport=ASGITransport(app=_app(), root_path="/admin"),
        base_url="http://test/admin",
    ) as client:
        page = await client.get("/bookings/?view=upcoming&status=confirmed")
        detail = await client.get(f"/bookings/{booking_id}")

    assert page.status_code == 200
    assert detail.status_code == 200
    assert "/admin/bookings/?view=upcoming&amp;status=confirmed&amp;cursor=next-cursor" in page.text
    assert f"/admin/bookings/{booking_id}" in page.text
    assert "/admin/chats/42" in page.text
    assert "Локальная проекция" in page.text
    assert "&lt;provider-id&gt;" in detail.text
    assert "<provider-id>" not in detail.text
    assert "/admin/bookings/?view=upcoming" in detail.text
    assert "/admin/chats/42" in detail.text
    assert "snapshot" not in detail.text
    assert "state" not in detail.text
    assert "payload" not in detail.text
    assert calls[0][0] == "database"
    assert calls[1][0:2] == ("database", booking_id)
    assert calls[1][2]["actor_id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "viewer"])
async def test_non_staff_is_rejected_before_booking_database(monkeypatch, role):
    async def current_user(_request):
        return _user(role)

    def forbidden_database():
        raise AssertionError("database must not be accessed before RBAC")

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", forbidden_database)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/bookings/")

    assert response.status_code == 403


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
@pytest.mark.parametrize(
    "query",
    ["?view=invalid", "?status=invalid", "?cursor=not-a-booking-cursor"],
)
async def test_invalid_filters_are_rejected_before_booking_database(monkeypatch, query):
    async def current_user(_request):
        return _user()

    def forbidden_database():
        raise AssertionError("invalid filters must not reach database")

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", forbidden_database)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get(f"/bookings/{query}")

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_booking_read_failures_are_unavailable_and_missing_is_not_found(monkeypatch):
    booking_id = uuid4()

    async def current_user(_request):
        return _user()

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("database secret must not be returned")

    async def detail_result(_database, value, **_kwargs):
        if value == booking_id:
            return None
        raise RuntimeError("audit failure must not be returned")

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_bookings", unavailable)
    monkeypatch.setattr(booking_routes, "get_booking_detail", detail_result)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        unavailable_response = await client.get("/bookings/")
        missing_response = await client.get(f"/bookings/{booking_id}")
        audit_response = await client.get(f"/bookings/{uuid4()}")

    assert unavailable_response.status_code == 503
    assert "database secret" not in unavailable_response.text
    assert missing_response.status_code == 404
    assert audit_response.status_code == 503
    assert "audit failure" not in audit_response.text


def test_booking_routes_do_not_depend_on_live_provider_clients():
    source = (booking_routes.__file__ and open(booking_routes.__file__, encoding="utf-8").read())

    assert "yclients" not in source.lower()
    assert "YCLIENTS" not in source
