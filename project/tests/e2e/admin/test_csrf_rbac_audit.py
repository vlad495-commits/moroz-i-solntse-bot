import importlib
from types import SimpleNamespace

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
async def test_prompt_save_reports_reload_delivery_failure(monkeypatch):
    created = {}

    async def current_user(_request):
        return user(username="o" * 65)

    async def create_version(**kwargs):
        created.update(kwargs)
        return 17

    async def reload_not_delivered(_version_id, _content):
        return prompt_routes.PROMPT_RELOAD_UNCONFIRMED

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(prompt_routes, "get_current_user", current_user)
    monkeypatch.setattr(prompt_routes, "_write_prompt", lambda _content: True)
    monkeypatch.setattr(prompt_routes.pdb, "create_version", create_version)
    monkeypatch.setattr(prompt_routes, "_publish_reload", reload_not_delivered)
    monkeypatch.setattr(prompt_routes, "record_audit", no_audit)

    response = await prompt_routes.prompt_save(
        SimpleNamespace(scope={}),
        content="new prompt",
        comment="test",
        csrf_token="known-csrf",
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/prompt/?saved=17&error=reload_failed"
    assert created["author"] == "o" * 64


@pytest.mark.asyncio
async def test_prompt_rollback_persists_username_not_user_object(monkeypatch):
    created = {}

    async def current_user(_request):
        return user()

    async def get_version(_version_id):
        return {"content": "same prompt\n"}

    async def create_version(**kwargs):
        created.update(kwargs)
        return 18

    async def reload_applied(_version_id, _content):
        return prompt_routes.PROMPT_RELOAD_APPLIED

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(prompt_routes, "get_current_user", current_user)
    monkeypatch.setattr(prompt_routes.pdb, "get_version", get_version)
    monkeypatch.setattr(prompt_routes.pdb, "create_version", create_version)
    monkeypatch.setattr(prompt_routes, "_read_prompt_snapshot", lambda: "same prompt\n")
    monkeypatch.setattr(prompt_routes, "_write_prompt", lambda _content: True)
    monkeypatch.setattr(prompt_routes, "_publish_reload", reload_applied)
    monkeypatch.setattr(prompt_routes, "record_audit", no_audit)

    response = await prompt_routes.prompt_rollback(
        SimpleNamespace(scope={}),
        version_id=17,
        csrf_token="known-csrf",
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/prompt/?saved=18"
    assert created["author"] == "owner"


@pytest.mark.asyncio
async def test_prompt_save_db_failure_keeps_active_file_unchanged(monkeypatch):
    async def current_user(_request):
        return user()

    async def fail_create_version(**_kwargs):
        raise RuntimeError("database unavailable")

    def write_must_not_be_called(_content):
        raise AssertionError("active prompt changed before version commit")

    monkeypatch.setattr(prompt_routes, "get_current_user", current_user)
    monkeypatch.setattr(prompt_routes.pdb, "create_version", fail_create_version)
    monkeypatch.setattr(prompt_routes, "_write_prompt", write_must_not_be_called)

    response = await prompt_routes.prompt_save(
        SimpleNamespace(scope={}),
        content="new prompt",
        comment="test",
        csrf_token="known-csrf",
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/prompt/?error=db_failed"


@pytest.mark.asyncio
async def test_prompt_save_write_failure_discards_unactivated_version(monkeypatch):
    discarded = []

    async def current_user(_request):
        return user()

    async def create_version(**_kwargs):
        return 18

    async def delete_version(version_id):
        discarded.append(version_id)

    monkeypatch.setattr(prompt_routes, "get_current_user", current_user)
    monkeypatch.setattr(prompt_routes.pdb, "create_version", create_version)
    monkeypatch.setattr(prompt_routes.pdb, "delete_version", delete_version)
    monkeypatch.setattr(prompt_routes, "_write_prompt", lambda _content: False)

    response = await prompt_routes.prompt_save(
        SimpleNamespace(scope={}),
        content="new prompt",
        comment="test",
        csrf_token="known-csrf",
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/prompt/?error=write_failed"
    assert discarded == [18]


@pytest.mark.asyncio
async def test_prompt_save_rejected_reload_restores_previous_file(
    monkeypatch, tmp_path
):
    prompt_file = tmp_path / "system.md"
    prompt_file.write_text("old prompt\n", encoding="utf-8")
    discarded = []

    async def current_user(_request):
        return user()

    async def create_version(**_kwargs):
        return 19

    async def delete_version(version_id):
        discarded.append(version_id)

    async def reject_reload(_version_id, _content):
        return prompt_routes.PROMPT_RELOAD_REJECTED

    monkeypatch.setattr(prompt_routes, "PROMPT_FILE", prompt_file)
    monkeypatch.setattr(prompt_routes, "get_current_user", current_user)
    monkeypatch.setattr(prompt_routes.pdb, "create_version", create_version)
    monkeypatch.setattr(prompt_routes.pdb, "delete_version", delete_version)
    monkeypatch.setattr(prompt_routes, "_publish_reload", reject_reload)

    response = await prompt_routes.prompt_save(
        SimpleNamespace(scope={}),
        content="rejected prompt",
        comment="test",
        csrf_token="known-csrf",
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/prompt/?error=reload_rejected"
    assert prompt_file.read_text(encoding="utf-8") == "old prompt\n"
    assert discarded == [19]


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
