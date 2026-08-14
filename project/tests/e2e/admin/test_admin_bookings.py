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
                "detail_id": booking_id,
                "customer_chat_id": 42,
                "customer_label": "Клиент #42",
                "starts_at": datetime(2026, 8, 15, 10, 0, tzinfo=UTC),
                "scheduled_end_at": None,
                "status": "confirmed",
                "status_label": "Подтверждена",
                "updated_at": datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
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
                "private_phone": "+79990000000",
                "raw_custom_field": "private custom field",
                "snapshot": "private snapshot",
                "state": "private state",
                "payload": "private payload",
                "status_raw": "private enum",
            }
        ],
        "has_more": True,
        "next_cursor": "next-cursor",
        "freshness": {
            "last_success_at": datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
            "stale": False,
        },
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
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_staff_can_read_safe_booking_list_and_detail(monkeypatch, role):
    booking_id = uuid4()
    calls = []

    async def current_user(_request):
        return _user(role)

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
        page = await client.get(
            "/bookings/?view=upcoming&status=confirmed&source=bot&reconciliation=mismatch"
        )
        detail = await client.get(f"/bookings/{booking_id}")

    assert page.status_code == 200
    assert detail.status_code == 200
    assert (
        "/admin/bookings/?view=upcoming&amp;status=confirmed&amp;source=bot"
        "&amp;reconciliation=mismatch&amp;cursor=next-cursor"
    ) in page.text
    assert f"/admin/bookings/{booking_id}" in page.text
    assert "/admin/chats/42" in page.text
    assert "Локальная проекция" in page.text
    assert "&lt;provider-id&gt;" in detail.text
    assert "<provider-id>" not in detail.text
    assert "/admin/bookings/?view=upcoming" in detail.text
    assert "/admin/chats/42" in detail.text
    assert "Создано ботом" in page.text
    assert "Другой канал" in page.text
    assert "Есть расхождение" in page.text
    assert "Последняя синхронизация" in page.text
    assert "&lt;client&gt;" in page.text
    assert "&lt;staff&gt;" in page.text
    assert "&lt;service&gt;" in page.text
    assert "<client>" not in page.text
    assert "<staff>" not in page.text
    assert "<service>" not in page.text
    assert "+79990000000" not in page.text
    assert "private custom field" not in page.text
    assert "snapshot" not in detail.text
    assert "state" not in detail.text
    assert "payload" not in detail.text
    assert calls[0][0] == "database"
    assert calls[0][1] == {
        "view": "upcoming",
        "status": "confirmed",
        "source": "bot",
        "reconciliation": "mismatch",
        "cursor": None,
    }
    assert calls[1][0:2] == ("database", booking_id)
    assert calls[1][2]["actor_id"] == 7


@pytest.mark.asyncio
async def test_empty_status_means_no_status_filter(monkeypatch):
    captured = {}

    async def current_user(_request):
        return _user()

    async def list_projection(database, **kwargs):
        captured.update({"database": database, **kwargs})
        return {"items": [], "has_more": False, "next_cursor": None}

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_bookings", list_projection)
    async with AsyncClient(
        transport=ASGITransport(app=_app(), root_path="/admin"),
        base_url="http://test/admin",
    ) as client:
        response = await client.get("/bookings/?status=")

    assert response.status_code == 200
    assert captured == {
        "database": "database",
        "view": "upcoming",
        "status": None,
        "source": "all",
        "reconciliation": "all",
        "cursor": None,
    }
    assert 'name="status"' in response.text
    assert '<option value="">Все статусы</option>' in response.text
    assert "?view=upcoming&amp;status=" not in response.text


@pytest.mark.asyncio
async def test_incompatible_customer_id_is_a_safe_non_link_label(monkeypatch):
    booking_id = uuid4()

    async def current_user(_request):
        return _user()

    async def list_projection(_database, **_kwargs):
        return {
            "items": [
                {
                    **_page(booking_id)["items"][0],
                    "customer_chat_id": None,
                    "customer_label": "Клиент",
                }
            ],
            "has_more": False,
            "next_cursor": None,
        }

    async def detail_projection(_database, value, **_kwargs):
        return {
            **_detail(value),
            "customer_chat_id": None,
            "customer_label": "Клиент",
        }

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_bookings", list_projection)
    monkeypatch.setattr(booking_routes, "get_booking_detail", detail_projection)
    async with AsyncClient(
        transport=ASGITransport(app=_app(), root_path="/admin"),
        base_url="http://test/admin",
    ) as client:
        page = await client.get("/bookings/")
        detail = await client.get(f"/bookings/{booking_id}")

    assert page.status_code == 200
    assert detail.status_code == 200
    assert "/admin/chats/" not in page.text
    assert "/admin/chats/" not in detail.text
    assert "Клиент" in page.text
    assert "Клиент" in detail.text


@pytest.mark.asyncio
async def test_yclients_only_rows_have_no_local_links_and_unknown_text_is_escaped(monkeypatch):
    async def current_user(_request):
        return _user()

    async def list_projection(_database, **_kwargs):
        return {
            "items": [
                {
                    **_page()["items"][0],
                    "id": None,
                    "detail_id": None,
                    "customer_chat_id": None,
                    "source": "other",
                    "source_label": "Другой канал",
                    "reconciliation_state": "yclients_only",
                    "reconciliation_label": "Только в YCLIENTS",
                    "client_name": "yclients-only-marker",
                    "staff_name": None,
                    "service_names": [],
                },
                {
                    **_page()["items"][0],
                    "source": "unknown",
                    "source_label": "<unknown-provider>",
                },
            ],
            "has_more": False,
            "next_cursor": None,
            "freshness": _page()["freshness"],
        }

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_bookings", list_projection)
    async with AsyncClient(
        transport=ASGITransport(app=_app(), root_path="/admin"),
        base_url="http://test/admin",
    ) as client:
        response = await client.get("/bookings/")

    assert response.status_code == 200
    yclients_only_row = response.text.split("yclients-only-marker", 1)[1].split(
        "</article>", 1
    )[0]
    assert "/admin/chats/" not in yclients_only_row
    assert "/admin/bookings/" not in yclients_only_row
    assert "&lt;unknown-provider&gt;" in response.text
    assert "<unknown-provider>" not in response.text


@pytest.mark.asyncio
async def test_stale_projection_freshness_is_visible(monkeypatch):
    async def current_user(_request):
        return _user()

    async def list_projection(_database, **_kwargs):
        page = _page()
        page["has_more"] = False
        page["freshness"] = {
            "last_success_at": datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
            "stale": True,
        }
        return page

    monkeypatch.setattr(booking_routes, "get_current_user", current_user)
    monkeypatch.setattr(booking_routes.database, "get_database", lambda: "database")
    monkeypatch.setattr(booking_routes, "list_bookings", list_projection)
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/bookings/")

    assert response.status_code == 200
    assert "Данные YCLIENTS могут быть устаревшими" in response.text


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
    [
        "?view=invalid",
        "?status=invalid",
        "?source=invalid",
        "?reconciliation=invalid",
        "?cursor=not-a-booking-cursor",
    ],
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
