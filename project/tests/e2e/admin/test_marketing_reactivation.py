from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from httpx import ASGITransport, AsyncClient

import auth
import reactivation_database as rdb
import reactivation_routes
from moroz.privacy import customer_lock_subject


def _user(role="owner"):
    return auth.AuthenticatedUser(
        id=7,
        username="owner",
        role=role,
        csrf_token="known-csrf",
        session_id="session",
    )


def _app():
    app = FastAPI()
    app.include_router(reactivation_routes.router)
    app.include_router(reactivation_routes.legacy_router)

    @app.exception_handler(auth._LoginRequired)
    async def login_required(_request, _error):
        return RedirectResponse("/login", status_code=302)

    return app


def _dashboard():
    return {
        "settings": {
            "mode": "dry_run",
            "active_version_id": None,
            "legal_status": "pending",
            "legal_reference": None,
            "legal_approved_at": None,
            "program_revision": 0,
            "updated_at": None,
        },
        "versions": [],
        "consents": [],
        "journeys": [],
        "outcomes": {},
        "legacy": {"campaigns": [], "deliveries": []},
        "pagination": {"page": 1, "has_next": False},
    }


@pytest.mark.asyncio
async def test_legacy_route_preserves_query():
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/reactivation/?status=active", follow_redirects=False
        )

    assert response.status_code == 307
    assert response.headers["location"] == "/marketing/?status=active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected"), [("owner", 200), ("admin", 403), (None, 302)]
)
async def test_marketing_page_is_owner_only(monkeypatch, role, expected):
    async def current_user(_request):
        if role is None:
            raise auth._LoginRequired
        return _user(role)

    async def dashboard(_database, *, actor_id, consent_id=None, page=1):
        assert actor_id == 7
        assert consent_id is None
        assert page == 1
        return _dashboard()

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "get_marketing_page_data", dashboard)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/marketing/", follow_redirects=False)

    assert response.status_code == expected
    if role is None:
        assert response.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_consent_lookup_accepts_only_opaque_consent_id(monkeypatch):
    consent_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    captured = []

    async def current_user(_request):
        return _user()

    async def page_data(_database, *, actor_id, consent_id, page):
        captured.append((actor_id, consent_id, page))
        return _dashboard()

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(
        reactivation_routes.rdb, "get_marketing_page_data", page_data
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get(f"/marketing/?consent_id={consent_id}&page=2")

    assert response.status_code == 200
    assert captured == [(7, consent_id, 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/marketing/versions",
        "/marketing/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/preview",
        "/marketing/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/test",
        "/marketing/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/activate",
        "/marketing/legal",
        "/marketing/mode",
        "/marketing/consents/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/revoke",
    ],
)
async def test_every_marketing_write_requires_csrf_before_database(
    monkeypatch, path
):
    async def current_user(_request):
        return _user()

    def database_must_not_run():
        raise AssertionError("database touched before CSRF")

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        reactivation_routes.database, "get_database", database_must_not_run
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(path, data={}, follow_redirects=False)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_version_form_rejects_invalid_reminder_without_database(monkeypatch):
    async def current_user(_request):
        return _user()

    def database_must_not_run():
        raise AssertionError("database touched for invalid policy")

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        reactivation_routes.database, "get_database", database_must_not_run
    )
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/marketing/versions",
            data={
                "csrf_token": "known-csrf",
                "reminder_after_days": "tomorrow",
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_activation_requires_exact_fresh_confirmation(monkeypatch):
    calls = []

    async def current_user(_request):
        return _user()

    async def activate(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "activate_version", activate)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        rejected = await client.post(
            "/marketing/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/activate",
            data={"csrf_token": "known-csrf", "confirmation": "активировать"},
        )
        accepted = await client.post(
            "/marketing/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/activate",
            data={"csrf_token": "known-csrf", "confirmation": "АКТИВИРОВАТЬ"},
            follow_redirects=False,
        )

    assert rejected.status_code == 400
    assert accepted.status_code == 302
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_active_mode_also_requires_exact_confirmation(monkeypatch):
    calls = []

    async def current_user(_request):
        return _user()

    async def set_mode(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "set_mode", set_mode)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        rejected = await client.post(
            "/marketing/mode",
            data={
                "csrf_token": "known-csrf",
                "mode": "active",
                "confirmation": "yes",
            },
        )
        accepted = await client.post(
            "/marketing/mode",
            data={
                "csrf_token": "known-csrf",
                "mode": "active",
                "confirmation": "АКТИВИРОВАТЬ",
            },
            follow_redirects=False,
        )

    assert rejected.status_code == 400
    assert accepted.status_code == 302
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_consent_revoke_uses_only_opaque_consent_id(monkeypatch):
    consent_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    calls = []

    async def current_user(_request):
        return _user()

    async def revoke(_database, *, consent_id):
        calls.append(consent_id)
        return {
            "id": consent_id,
            "active": False,
            "consent_version": "marketing-v1",
        }

    async def audit(**_values):
        return None

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(
        reactivation_routes.rdb, "revoke_marketing_consent_by_id", revoke
    )
    monkeypatch.setattr(reactivation_routes, "record_audit", audit)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/marketing/consents/{consent_id}/revoke",
            data={"csrf_token": "known-csrf"},
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"] == "/marketing/?consent=revoked"
    assert calls == [consent_id]
    assert "telegram" not in response.headers["location"]


@pytest.mark.asyncio
async def test_consent_revoke_resolves_identity_and_applies_both_events_in_one_transaction(
    monkeypatch,
):
    consent_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transaction = SimpleTransaction()
    connection = SimpleConnection(consent_id, transaction)
    service = SimpleConsentService()
    monkeypatch.setattr(rdb, "ConsentService", lambda _database: service)

    result = await rdb.revoke_marketing_consent_by_id(
        SimpleDatabase(connection), consent_id=consent_id
    )

    assert transaction.entered and transaction.exited
    assert connection.initial_lookup == consent_id
    assert connection.locked_subject == customer_lock_subject("42")
    assert [call[0] for call in service.calls] == ["revoke", "suppress"]
    assert all(call[1]["channel"] == "telegram" for call in service.calls)
    assert all(call[1]["user_id"] == "42" for call in service.calls)
    assert all(call[1]["connection"] is connection for call in service.calls)
    assert service.calls[1][1]["reason"] == "admin_revoke"
    assert result["id"] == consent_id


class SimpleTransaction:
    entered = False
    exited = False

    async def __aenter__(self):
        self.entered = True

    async def __aexit__(self, *_args):
        self.exited = True


class SimpleConnection:
    def __init__(self, consent_id, transaction):
        self.consent_id = consent_id
        self._transaction = transaction
        self.initial_lookup = None
        self.locked_subject = None
        self.lookup_count = 0

    def transaction(self):
        return self._transaction

    async def fetchrow(self, query, *args):
        self.lookup_count += 1
        assert "WHERE id = $1" in query
        assert args[0] == self.consent_id
        if self.lookup_count == 1:
            self.initial_lookup = args[0]
        return {
            "id": self.consent_id,
            "channel": "telegram",
            "user_id": "42",
            "consent_version": "marketing-v1",
            "active": self.lookup_count < 3,
        }

    async def execute(self, query, *args):
        assert "pg_advisory_xact_lock" in query
        self.locked_subject = args[0]


class SimpleAcquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class SimpleDatabase:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return SimpleAcquire(self.connection)


class SimpleConsentService:
    def __init__(self):
        self.calls = []

    async def revoke_marketing(self, **values):
        self.calls.append(("revoke", values))

    async def suppress_marketing(self, **values):
        self.calls.append(("suppress", values))
