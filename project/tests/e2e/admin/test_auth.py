import importlib

import pytest
from httpx import ASGITransport, AsyncClient


admin_app = importlib.import_module("app")
auth = importlib.import_module("auth")


@pytest.mark.asyncio
async def test_login_page_requires_totp_code():
    transport = ASGITransport(app=admin_app.app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/login")

    assert response.status_code == 200
    assert 'name="totp_code"' in response.text


@pytest.mark.asyncio
async def test_login_submit_passes_totp_and_sets_session_cookie(monkeypatch):
    seen = {}

    async def fake_authenticate(username, password, totp_code):
        seen["credentials"] = (username, password, totp_code)
        return auth.AuthenticatedUser(
            id=7,
            username="owner",
            role="owner",
            csrf_token="csrf-token",
            session_id="session-id",
        )

    monkeypatch.setattr(admin_app, "authenticate_admin", fake_authenticate)
    transport = ASGITransport(app=admin_app.app)

    async with AsyncClient(
        transport=transport,
        base_url="https://admin.test",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            "/login",
            data={
                "username": "owner",
                "password": "secret",
                "totp_code": "123456",
            },
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert seen["credentials"] == ("owner", "secret", "123456")
    cookie = response.headers["set-cookie"]
    assert auth.SESSION_COOKIE_NAME in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


@pytest.mark.asyncio
async def test_login_submit_rejects_failed_authentication(monkeypatch):
    async def fake_authenticate(username, password, totp_code):
        return None

    monkeypatch.setattr(admin_app, "authenticate_admin", fake_authenticate)
    transport = ASGITransport(app=admin_app.app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            "/login",
            data={"username": "owner", "password": "bad", "totp_code": "000000"},
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/login?error=invalid"


def test_session_token_round_trip_returns_authenticated_user():
    user = auth.AuthenticatedUser(
        id=7,
        username="owner",
        role="owner",
        csrf_token="csrf-token",
        session_id="session-id",
    )

    token = auth.create_session_token(user)
    restored = auth.verify_session_token(token)

    assert restored == user
    assert str(restored) == "owner"


@pytest.mark.asyncio
async def test_bootstrap_env_login_only_works_without_db_users(monkeypatch):
    async def no_db_users():
        return 0

    async def no_db_user(username):
        return None

    monkeypatch.setattr(auth.user_repository, "count_admin_users", no_db_users)
    monkeypatch.setattr(auth.user_repository, "get_user_by_username", no_db_user)
    monkeypatch.setenv("ADMIN_USERNAME", "bootstrap")
    monkeypatch.setenv("ADMIN_PASSWORD", "bootstrap-secret")

    user = await auth.authenticate_admin("bootstrap", "bootstrap-secret", "")

    assert user is not None
    assert user.username == "bootstrap"
    assert user.role == "owner"
