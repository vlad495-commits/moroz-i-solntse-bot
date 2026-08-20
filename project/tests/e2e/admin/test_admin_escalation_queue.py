import importlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
escalation_routes = importlib.import_module("escalation_routes")
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
    assert f"/escalations/{escalation_id}/reply" in response.text
    assert 'name="csrf_token" value="known-csrf"' in response.text
    assert 'name="reply_token"' in response.text
    assert 'name="reply_text"' in response.text
    assert 'maxlength="4096"' in response.text
    assert "Закрыть и вернуть бота" not in response.text
    assert "<script>небезопасно</script>" not in response.text
    assert "&lt;script&gt;" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_staff_can_queue_reply(monkeypatch, role):
    escalation_id = uuid4()
    reply_token = uuid4()
    captured = {}

    async def current_user(_request):
        return _user(role)

    async def enqueue(value, **kwargs):
        captured.update({"id": value, **kwargs})
        return "queued", uuid4()

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        escalation_routes.database,
        "enqueue_escalation_reply",
        enqueue,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/escalations/{escalation_id}/reply",
            data={
                "csrf_token": "known-csrf",
                "reply_token": str(reply_token),
                "reply_text": "  Ответ клиенту  ",
            },
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/escalations/?reply=queued"
    assert captured["id"] == escalation_id
    assert captured["actor_id"] == 7
    assert captured["reply_token"] == reply_token
    assert captured["text"] == "Ответ клиенту"


@pytest.mark.asyncio
async def test_reply_rejects_bad_csrf_before_database(monkeypatch):
    async def current_user(_request):
        return _user("admin")

    async def forbidden_database(*_args, **_kwargs):
        raise AssertionError("database must not be called before CSRF passes")

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        escalation_routes.database, "enqueue_escalation_reply", forbidden_database
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/escalations/{uuid4()}/reply",
            data={
                "reply_token": str(uuid4()),
                "reply_text": "Ответ",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_reply_unknown_escalation_returns_404(monkeypatch):
    async def current_user(_request):
        return _user()

    async def not_found(*_args, **_kwargs):
        return "not_found", None

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        escalation_routes.database,
        "enqueue_escalation_reply",
        not_found,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/escalations/{uuid4()}/reply",
            data={
                "csrf_token": "known-csrf",
                "reply_token": str(uuid4()),
                "reply_text": "Ответ",
            },
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_reply_inactive_escalation_returns_409(monkeypatch):
    async def current_user(_request):
        return _user("admin")

    async def inactive(*_args, **_kwargs):
        return "inactive", None

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        escalation_routes.database,
        "enqueue_escalation_reply",
        inactive,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/escalations/{uuid4()}/reply",
            data={
                "csrf_token": "known-csrf",
                "reply_token": str(uuid4()),
                "reply_text": "Ответ",
            },
        )

    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply_text", "expected_status"),
    [("   ", 422), ("x" * 4097, 422)],
)
async def test_reply_validates_text_before_database(
    monkeypatch,
    reply_text,
    expected_status,
):
    async def current_user(_request):
        return _user("admin")

    async def forbidden_database(*_args, **_kwargs):
        raise AssertionError("invalid reply must not reach database")

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        escalation_routes.database,
        "enqueue_escalation_reply",
        forbidden_database,
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/escalations/{uuid4()}/reply",
            data={
                "csrf_token": "known-csrf",
                "reply_token": str(uuid4()),
                "reply_text": reply_text,
            },
        )

    assert response.status_code == expected_status


@pytest.mark.asyncio
async def test_anonymous_queue_redirects_to_login(monkeypatch):
    async def login_required(_request):
        raise auth._LoginRequired

    monkeypatch.setattr(escalation_routes, "get_current_user", login_required)
    async with AsyncClient(
        transport=ASGITransport(app=admin_app.app),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/escalations/")

    assert response.status_code == 302
    assert response.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_queue_links_and_redirect_respect_root_path(monkeypatch):
    escalation_id = uuid4()

    async def current_user(_request):
        return _user("admin")

    async def get_open(*, limit):
        assert limit == 100
        return [{
            "id": escalation_id,
            "customer_id": "42",
            "source": "Система",
            "reason": "Требуется помощь администратора",
            "created_at": datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
            "human_mode_enabled": True,
        }]

    async def enqueue(*_args, **_kwargs):
        return "queued", uuid4()

    monkeypatch.setattr(escalation_routes, "get_current_user", current_user)
    monkeypatch.setattr(escalation_routes.database, "get_open_escalations", get_open)
    monkeypatch.setattr(
        escalation_routes.database,
        "enqueue_escalation_reply",
        enqueue,
    )
    app = _app()
    async with AsyncClient(
        transport=ASGITransport(app=app, root_path="/admin"),
        base_url="http://test/admin",
    ) as client:
        page = await client.get("/escalations/")
        response = await client.post(
            f"/escalations/{escalation_id}/reply",
            data={
                "csrf_token": "known-csrf",
                "reply_token": str(uuid4()),
                "reply_text": "Ответ",
            },
        )

    assert '/admin/chats/42' in page.text
    assert f'/admin/escalations/{escalation_id}/reply' in page.text
    assert response.headers["location"] == "/admin/escalations/?reply=queued"
