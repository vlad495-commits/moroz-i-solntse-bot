import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
admin_app = importlib.import_module("app")
bot_control_routes = importlib.import_module("bot_control_routes")
customer_data_routes = importlib.import_module("customer_data_routes")
statistics_routes = importlib.import_module("statistics_routes")
rbac = importlib.import_module("rbac")
audit_repository = importlib.import_module("audit_repository")


def user(role="owner", csrf_token="known-csrf", username="owner"):
    return auth.AuthenticatedUser(
        id=7,
        username=username,
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
async def test_stats_page_rejects_admin_role_before_stats_read(monkeypatch):
    async def current_user(_request):
        return user(role="admin")

    async def stats_must_not_be_called(_period):
        raise AssertionError("stats should not be read before RBAC passes")

    monkeypatch.setattr(statistics_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        statistics_routes.database,
        "get_statistics_snapshot",
        stats_must_not_be_called,
    )

    with pytest.raises(HTTPException) as denied:
        await statistics_routes.statistics_page(object())

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



def test_owner_only_navigation_links_are_hidden_from_admin_role():
    base = (admin_app._BASE_DIR / "templates" / "base.html").read_text(encoding="utf-8")

    assert "{% if user.role == 'owner' %}" in base
    assert "/stats" in base
    assert "/prompt/" not in base
    assert "/bot-control/" in base
    assert "/marketing/" in base
    assert "Маркетинговые коммуникации" in base


def test_review_cases_module_is_not_exposed():
    base = (admin_app._BASE_DIR / "templates" / "base.html").read_text(
        encoding="utf-8"
    )
    paths = {route.path for route in admin_app.app.routes}

    assert "Review кейсов" not in base
    assert not any(path.startswith("/review") for path in paths)


@pytest.mark.asyncio
async def test_customer_data_delete_rejects_admin_before_redis(monkeypatch):
    async def current_user(_request):
        return user(role="admin")

    async def forbidden_redis():
        raise AssertionError("redis must not be touched before owner RBAC")

    monkeypatch.setattr(customer_data_routes, "get_current_user", current_user)
    monkeypatch.setattr(customer_data_routes, "_redis_client", forbidden_redis)
    app = FastAPI()
    app.include_router(customer_data_routes.router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/customer-data/delete",
            data={
                "chat_id": "42",
                "csrf_token": "known-csrf",
                "confirmation": "УДАЛИТЬ",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_customer_data_delete_rejects_bad_confirmation_before_redis(
    monkeypatch,
):
    async def current_user(_request):
        return user()

    async def forbidden_redis():
        raise AssertionError("redis must not be touched before confirmation")

    monkeypatch.setattr(customer_data_routes, "get_current_user", current_user)
    monkeypatch.setattr(customer_data_routes, "_redis_client", forbidden_redis)
    app = FastAPI()
    app.include_router(customer_data_routes.router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/customer-data/delete",
            data={
                "chat_id": "42",
                "csrf_token": "known-csrf",
                "confirmation": "нет",
            },
        )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_customer_data_delete_owner_redirects_without_identifier(
    monkeypatch,
):
    deleted = {}

    class FakeRedis:
        closed = False

        async def aclose(self):
            self.closed = True

    cache = FakeRedis()

    async def current_user(_request):
        return user()

    async def redis_client():
        return cache

    async def delete_customer_data(**kwargs):
        deleted.update(kwargs)
        return SimpleNamespace(status="deleted")

    monkeypatch.setattr(customer_data_routes, "get_current_user", current_user)
    monkeypatch.setattr(customer_data_routes, "_redis_client", redis_client)
    monkeypatch.setattr(
        customer_data_routes, "delete_customer_data", delete_customer_data
    )
    monkeypatch.setattr(customer_data_routes.database, "_pool", object())
    app = FastAPI()
    app.include_router(customer_data_routes.router)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/customer-data/delete",
            data={
                "chat_id": "42",
                "csrf_token": "known-csrf",
                "confirmation": "УДАЛИТЬ",
            },
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/?deleted=deleted"
    assert "42" not in response.headers["location"]
    assert deleted["chat_id"] == 42
    assert cache.closed is True


def test_customer_data_danger_zone_is_owner_only():
    template = (
        admin_app._BASE_DIR / "templates" / "chat_detail.html"
    ).read_text(encoding="utf-8")

    assert "{% if user.role == 'owner' %}" in template
    assert "/customer-data/delete" in template
    assert 'name="csrf_token"' in template
    assert 'name="confirmation"' in template


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
