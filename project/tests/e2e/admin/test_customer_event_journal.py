import importlib
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
admin_app = importlib.import_module("app")


def _user(role):
    return auth.AuthenticatedUser(
        id=7,
        username=role,
        role=role,
        csrf_token="csrf-token",
        session_id="session-id",
    )


def _detail():
    return {
        "chat_id": 42,
        "user_id": 7,
        "username": "client",
        "messages": [],
        "stats": {},
    }


def _events():
    return {
        "items": [
            {
                "event_id": "message:1",
                "occurred_at": datetime(2026, 8, 13, 20, 0, tzinfo=UTC),
                "category": "message",
                "kind": "message.user",
                "title": "Сообщение клиента",
                "description": "<script>alert('journal')</script>",
                "status": None,
            }
        ],
        "offset": 0,
        "next_offset": 50,
        "previous_offset": None,
        "has_more": True,
        "anchor": datetime(2026, 8, 13, 20, 1, tzinfo=UTC),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_chat_detail_renders_safe_event_page_for_both_roles(
    monkeypatch, role
):
    async def current_user(_request):
        return _user(role)

    async def get_detail(_chat_id):
        return _detail()

    async def get_events(chat_id, *, limit, offset, anchor):
        assert (chat_id, limit, offset, anchor) == (42, 50, 0, None)
        return _events()

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(admin_app, "get_current_user", current_user)
    monkeypatch.setattr(admin_app.database, "get_chat_detail", get_detail)
    monkeypatch.setattr(admin_app.database, "get_customer_events", get_events)
    monkeypatch.setattr(admin_app, "record_audit", no_audit)

    async with AsyncClient(
        transport=ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/chats/42")

    assert response.status_code == 200
    assert "События клиента" in response.text
    assert "Сообщение клиента" in response.text
    assert "events_offset=50" in response.text
    assert "events_anchor=" in response.text
    assert "<script>alert('journal')</script>" not in response.text
    assert "&lt;script&gt;alert" in response.text
    if role == "owner":
        assert "Удалить локальные данные" in response.text
    else:
        assert "Удалить локальные данные" not in response.text


@pytest.mark.asyncio
async def test_chat_detail_rejects_negative_event_offset(monkeypatch):
    async def current_user(_request):
        return _user("owner")

    monkeypatch.setattr(admin_app, "get_current_user", current_user)
    async with AsyncClient(
        transport=ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/chats/42?events_offset=-1")

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_chat_redirects_without_loading_events(monkeypatch):
    async def current_user(_request):
        return _user("owner")

    async def missing_detail(_chat_id):
        return None

    async def forbidden_events(*_args, **_kwargs):
        raise AssertionError("events must not load for an unknown chat")

    monkeypatch.setattr(admin_app, "get_current_user", current_user)
    monkeypatch.setattr(admin_app.database, "get_chat_detail", missing_detail)
    monkeypatch.setattr(
        admin_app.database,
        "get_customer_events",
        forbidden_events,
    )
    async with AsyncClient(
        transport=ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/chats/404")

    assert response.status_code == 302
    assert response.headers["location"] == "/"
