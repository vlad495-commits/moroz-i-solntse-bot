import importlib
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient


admin_app = importlib.import_module("app")
auth = importlib.import_module("auth")


class RequestStub:
    def __init__(self, token):
        self.cookies = {auth.SESSION_COOKIE_NAME: token}


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
async def test_login_submit_forces_secure_cookie_when_configured(monkeypatch):
    async def fake_authenticate(_username, _password, _totp_code):
        return auth.AuthenticatedUser(
            id=1,
            username="owner",
            role="owner",
            csrf_token="csrf-token",
            session_id="session-id",
        )

    monkeypatch.setattr(admin_app, "authenticate_admin", fake_authenticate)
    monkeypatch.setattr(admin_app, "create_session_token", lambda _user: "token")
    monkeypatch.setattr(admin_app, "ADMIN_COOKIE_SECURE", True)

    response = await admin_app.login_submit(
        request=SimpleNamespace(url=SimpleNamespace(scheme="http")),
        username="owner",
        password="secret",
        totp_code="123456",
    )

    assert "Secure" in response.headers["set-cookie"]


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
async def test_current_user_rejects_cookie_when_db_session_is_missing(monkeypatch):
    async def missing_session(session_id):
        return None

    token = auth.create_session_token(
        auth.AuthenticatedUser(
            id=7,
            username="owner",
            role="owner",
            csrf_token="csrf-token",
            session_id="stale-session",
        )
    )
    monkeypatch.setattr(auth.user_repository, "get_active_session", missing_session)

    with pytest.raises(auth._LoginRequired):
        await auth.get_current_user(RequestStub(token))


@pytest.mark.asyncio
async def test_current_user_uses_active_db_session(monkeypatch):
    async def active_session(session_id):
        return {
            "session_id": session_id,
            "user_id": 7,
            "username": "owner",
            "role": "owner",
            "csrf_token": "fresh-csrf",
        }

    token = auth.create_session_token(
        auth.AuthenticatedUser(
            id=7,
            username="owner",
            role="owner",
            csrf_token="old-csrf",
            session_id="live-session",
        )
    )
    monkeypatch.setattr(auth.user_repository, "get_active_session", active_session)

    user = await auth.get_current_user(RequestStub(token))

    assert user == auth.AuthenticatedUser(
        id=7,
        username="owner",
        role="owner",
        csrf_token="fresh-csrf",
        session_id="live-session",
    )


@pytest.mark.asyncio
async def test_logout_deletes_db_session(monkeypatch):
    deleted = []
    token = auth.create_session_token(
        auth.AuthenticatedUser(
            id=7,
            username="owner",
            role="owner",
            csrf_token="csrf-token",
            session_id="session-to-delete",
        )
    )

    async def delete_session(session_id):
        deleted.append(session_id)

    monkeypatch.setattr(admin_app.user_repository, "delete_session", delete_session)
    transport = ASGITransport(app=admin_app.app)

    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        client.cookies.set(auth.SESSION_COOKIE_NAME, token)
        response = await client.get("/logout")

    assert response.status_code == 302
    assert response.headers["location"] == "/login"
    assert deleted == ["session-to-delete"]


@pytest.mark.asyncio
async def test_bootstrap_env_login_rejects_default_credentials(monkeypatch):
    async def no_db_users():
        return 0

    async def no_db_user(username):
        return None

    monkeypatch.setattr(auth.user_repository, "count_admin_users", no_db_users)
    monkeypatch.setattr(auth.user_repository, "get_user_by_username", no_db_user)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")

    user = await auth.authenticate_admin("admin", "admin", "")

    assert user is None


@pytest.mark.asyncio
async def test_bootstrap_env_login_only_works_with_explicit_safe_values(monkeypatch):
    async def no_db_users():
        return 0

    async def no_db_user(username):
        return None

    monkeypatch.setattr(auth.user_repository, "count_admin_users", no_db_users)
    monkeypatch.setattr(auth.user_repository, "get_user_by_username", no_db_user)
    monkeypatch.setattr(auth, "SESSION_SECRET", "local-explicit-secret")
    monkeypatch.setenv("ADMIN_USERNAME", "bootstrap")
    monkeypatch.setenv("ADMIN_PASSWORD", "bootstrap-secret")

    user = await auth.authenticate_admin("bootstrap", "bootstrap-secret", "")

    assert user is not None
    assert user.username == "bootstrap"
    assert user.role == "owner"


@pytest.mark.asyncio
async def test_bootstrap_env_login_fails_closed_when_db_pool_is_missing(monkeypatch):
    monkeypatch.setattr(auth.user_repository.database, "_pool", None)
    monkeypatch.setattr(auth, "SESSION_SECRET", "local-explicit-secret")
    monkeypatch.setenv("ADMIN_USERNAME", "bootstrap")
    monkeypatch.setenv("ADMIN_PASSWORD", "bootstrap-secret")

    user = await auth.authenticate_admin("bootstrap", "bootstrap-secret", "")

    assert user is None
