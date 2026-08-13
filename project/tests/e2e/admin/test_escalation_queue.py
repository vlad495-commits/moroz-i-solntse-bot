import importlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
escalation_routes = importlib.import_module("escalation_routes")


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
    app.include_router(escalation_routes.router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_escalation_queue_is_safe_and_available_to_staff(monkeypatch, role):
    escalation_id = uuid4()

    async def current_user(_request):
        return _user(role)

    async def get_open(*, limit):
        assert limit == 100
        return [
            {
                "id": escalation_id,
                "customer_id": "42",
                "source": "Система",
                "reason": "<script>небезопасно</script>",
                "created_at": datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
                "human_mode_enabled": True,
            }
        ]

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(escalation_routes.database, "get_open_escalations", get_open)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/escalations/")

    assert response.status_code == 200
    assert "Эскалации" in response.text
    assert "/chats/42" in response.text
    assert f"/escalations/{escalation_id}/resolve" in response.text
    assert 'name="csrf_token" value="known-csrf"' in response.text
    assert "<script>небезопасно</script>" not in response.text
    assert "&lt;script&gt;" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_staff_can_resolve_escalation(monkeypatch, role):
    escalation_id = uuid4()
    captured = {}

    async def current_user(_request):
        return _user(role)

    async def resolve(value, **kwargs):
        captured.update({"id": value, **kwargs})
        return "resolved"

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(escalation_routes.database, "resolve_escalation", resolve)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/escalations/{escalation_id}/resolve",
            data={"csrf_token": "known-csrf"},
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/escalations/?resolved=resolved"
    assert captured["id"] == escalation_id
    assert captured["actor_id"] == 7


@pytest.mark.asyncio
async def test_resolve_rejects_bad_csrf_before_database(monkeypatch):
    async def current_user(_request):
        return _user("admin")

    async def forbidden_database(*_args, **_kwargs):
        raise AssertionError("database must not be called before CSRF passes")

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        escalation_routes.database, "resolve_escalation", forbidden_database
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(f"/escalations/{uuid4()}/resolve", data={})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_resolve_unknown_escalation_returns_404(monkeypatch):
    async def current_user(_request):
        return _user()

    async def not_found(*_args, **_kwargs):
        return "not_found"

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(escalation_routes.database, "resolve_escalation", not_found)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/escalations/{uuid4()}/resolve",
            data={"csrf_token": "known-csrf"},
        )

    assert response.status_code == 404
