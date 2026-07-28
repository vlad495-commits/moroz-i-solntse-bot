import importlib

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
admin_app = importlib.import_module("app")
bot_control_routes = importlib.import_module("bot_control_routes")
prompt_routes = importlib.import_module("prompt_routes")
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

    async def current_user(_request):
        return user()

    monkeypatch.setattr(bot_control_routes, "get_current_user", current_user)
    monkeypatch.setattr(bot_control_routes, "_redis_client", redis_must_not_be_called)
    app = FastAPI()
    app.include_router(bot_control_routes.router)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/bot-control/toggle", data={})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_bot_toggle_rejects_admin_role_before_redis(monkeypatch):
    async def redis_must_not_be_called():
        raise AssertionError("redis should not be touched before RBAC passes")

    async def current_user(_request):
        return user(role="admin")

    monkeypatch.setattr(bot_control_routes, "get_current_user", current_user)
    monkeypatch.setattr(bot_control_routes, "_redis_client", redis_must_not_be_called)
    app = FastAPI()
    app.include_router(bot_control_routes.router)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/bot-control/toggle",
            data={"csrf_token": "known-csrf"},
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_prompt_save_rejects_admin_role_before_file_write(monkeypatch):
    async def current_user(_request):
        return user(role="admin")

    def write_must_not_be_called(_content):
        raise AssertionError("prompt file should not be written before RBAC passes")

    monkeypatch.setattr(prompt_routes, "get_current_user", current_user)
    monkeypatch.setattr(prompt_routes, "_write_prompt", write_must_not_be_called)

    with pytest.raises(HTTPException) as denied:
        await prompt_routes.prompt_save(
            object(),
            content="new prompt",
            comment="test",
            csrf_token="known-csrf",
        )

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_prompt_rollback_rejects_admin_role_before_db_read(monkeypatch):
    async def current_user(_request):
        return user(role="admin")

    async def db_must_not_be_called(_version_id):
        raise AssertionError("prompt version should not be read before RBAC passes")

    monkeypatch.setattr(prompt_routes, "get_current_user", current_user)
    monkeypatch.setattr(prompt_routes.pdb, "get_version", db_must_not_be_called)

    with pytest.raises(HTTPException) as denied:
        await prompt_routes.prompt_rollback(
            object(),
            version_id=1,
            csrf_token="known-csrf",
        )

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_stats_page_rejects_admin_role_before_stats_read(monkeypatch):
    async def current_user(_request):
        return user(role="admin")

    async def stats_must_not_be_called():
        raise AssertionError("stats should not be read before RBAC passes")

    monkeypatch.setattr(admin_app, "get_current_user", current_user)
    monkeypatch.setattr(admin_app.database, "get_global_stats", stats_must_not_be_called)

    with pytest.raises(HTTPException) as denied:
        await admin_app.stats_page(object())

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_bot_control_page_rejects_admin_role_before_redis(monkeypatch):
    async def redis_must_not_be_called():
        raise AssertionError("redis should not be touched before RBAC passes")

    async def current_user(_request):
        return user(role="admin")

    monkeypatch.setattr(bot_control_routes, "get_current_user", current_user)
    monkeypatch.setattr(bot_control_routes, "_redis_client", redis_must_not_be_called)

    with pytest.raises(HTTPException) as denied:
        await bot_control_routes.bot_control_page(object())

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_prompt_editor_rejects_admin_role_before_prompt_read(monkeypatch):
    async def current_user(_request):
        return user(role="admin")

    def prompt_must_not_be_read():
        raise AssertionError("prompt file should not be read before RBAC passes")

    monkeypatch.setattr(prompt_routes, "get_current_user", current_user)
    monkeypatch.setattr(prompt_routes, "_read_current_prompt", prompt_must_not_be_read)

    with pytest.raises(HTTPException) as denied:
        await prompt_routes.prompt_editor(object())

    assert denied.value.status_code == 403


def test_owner_only_navigation_links_are_hidden_from_admin_role():
    base = (admin_app._BASE_DIR / "templates" / "base.html").read_text(encoding="utf-8")

    assert "{% if user.role == 'owner' %}" in base
    assert "/stats" in base
    assert "/prompt/" in base
    assert "/bot-control/" in base


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
