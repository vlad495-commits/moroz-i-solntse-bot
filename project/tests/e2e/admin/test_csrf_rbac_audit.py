import importlib

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
bot_control_routes = importlib.import_module("bot_control_routes")
rbac = importlib.import_module("rbac")
audit_repository = importlib.import_module("audit_repository")


def user(role="owner", csrf_token="known-csrf"):
    return auth.AuthenticatedUser(
        id=7,
        username="owner",
        role=role,
        csrf_token=csrf_token,
        session_id="session-id",
    )


def test_validate_csrf_rejects_missing_or_wrong_token():
    with pytest.raises(HTTPException) as missing:
        rbac.validate_csrf(user(), "")
    with pytest.raises(HTTPException) as wrong:
        rbac.validate_csrf(user(), "wrong")

    assert missing.value.status_code == 403
    assert wrong.value.status_code == 403


def test_require_role_rejects_disallowed_role():
    with pytest.raises(HTTPException) as denied:
        rbac.require_role(user(role="admin"), {"owner"})

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_bot_toggle_rejects_missing_csrf_before_redis(monkeypatch):
    async def redis_must_not_be_called():
        raise AssertionError("redis should not be touched before CSRF passes")

    monkeypatch.setattr(bot_control_routes, "get_current_user", lambda request: user())
    monkeypatch.setattr(bot_control_routes, "_redis_client", redis_must_not_be_called)
    app = FastAPI()
    app.include_router(bot_control_routes.router)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/bot-control/toggle", data={})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_record_audit_inserts_append_only_event(monkeypatch):
    calls = []

    class FakeConnection:
        async def execute(self, query, *args):
            calls.append((query, args))

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    monkeypatch.setattr(audit_repository.database, "_pool", FakePool())

    await audit_repository.record_audit(
        actor_id=7,
        action="bot.pause",
        object_type="bot_control",
        object_id=None,
        before={"paused": False},
        after={"paused": True},
        ip_address="127.0.0.1",
        user_agent="pytest",
    )

    assert len(calls) == 1
    query, args = calls[0]
    assert "INSERT INTO admin_audit_events" in query
    assert args[:3] == (7, "bot.pause", "bot_control")
