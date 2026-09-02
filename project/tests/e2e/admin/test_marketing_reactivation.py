from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from httpx import ASGITransport, AsyncClient

import auth
import reactivation_database as rdb
import reactivation_routes
from moroz.privacy import customer_lock_subject
from moroz.reactivation.repository import ActivationBlocked


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
        "readiness": {"proven_consents": 0, "yclients_current": 0, "yclients_ready": False},
        "consents": [],
        "consent_events": [],
        "journeys": [],
        "outcomes": {},
        "funnel": {
            "journey_started": 0,
            "main_sent": 0,
            "reminder_sent": 0,
            "replied_7d": 0,
            "booked_14d": 0,
            "completed_30d": 0,
            "opted_out": 0,
            "suppressed": 0,
            "escalated": 0,
            "failed": 0,
            "delivery_unknown": 0,
        },
        "latest_preview_eligible": None,
        "filters": {"period": 30, "outcome": "all", "delivery": "all"},
        "legacy": {"campaigns": [], "deliveries": []},
        "pagination": {"page": 1, "has_next": False},
    }


def _version(status="draft"):
    return {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "version_number": 3,
        "status": status,
        "inactivity_days": 90,
        "reminder_after_days": None,
        "cooldown_days": 90,
        "main_text": "Давно вас не видели. Будем рады новой встрече!",
        "reminder_text": "Напоминаем, что будем рады вас видеть.",
        "preview_counts": None,
        "preview_created_at": None,
        "preview_history_watermark": None,
        "preview_recent_watermark": None,
        "test_sent_at": None,
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
async def test_legacy_route_preserves_mounted_admin_root_and_query():
    parent = FastAPI()
    parent.mount("/admin", _app())
    async with AsyncClient(
        transport=ASGITransport(app=parent), base_url="http://test"
    ) as client:
        response = await client.get(
            "/admin/reactivation/?status=active", follow_redirects=False
        )

    assert response.status_code == 307
    assert response.headers["location"] == "/admin/marketing/?status=active"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected"), [("owner", 200), ("admin", 403), (None, 302)]
)
async def test_marketing_page_is_owner_only(monkeypatch, role, expected):
    async def current_user(_request):
        if role is None:
            raise auth._LoginRequired
        return _user(role)

    async def dashboard(_database, *, actor_id, consent_id=None, page=1, **filters):
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

    async def page_data(_database, *, actor_id, consent_id, page, **filters):
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
async def test_dashboard_renders_honest_funnel_and_rejects_unbounded_filters(
    monkeypatch,
):
    captured = []

    async def current_user(_request):
        return _user()

    async def page_data(*_args, **kwargs):
        captured.append(kwargs)
        data = _dashboard()
        data["latest_preview_eligible"] = 12
        data["funnel"].update(
            main_sent=10,
            replied_7d=4,
            booked_14d=2,
            completed_30d=1,
            failed=1,
            delivery_unknown=1,
        )
        data["settings"]["mode"] = "paused"
        return data

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "get_marketing_page_data", page_data)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get(
            "/marketing/?period=7&outcome=booked&delivery=delivery_unknown"
        )
        invalid = await client.get("/marketing/?period=365")

    assert response.status_code == 200
    assert captured[0]["period"] == 7
    assert captured[0]["outcome"] == "booked"
    assert captured[0]["delivery"] == "delivery_unknown"
    assert "Результаты реактивации" in response.text
    assert "Подходят" in response.text
    assert "Принято Telegram" in response.text
    assert "Статус доставки неизвестен" in response.text
    assert "Программа остановлена" in response.text
    assert "Доставлено" not in response.text
    assert "Прочитано" not in response.text
    assert invalid.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "active_version", "shows_stop", "shows_resume"),
    [
        ("active", True, True, False),
        ("paused", True, False, True),
        ("paused", False, False, False),
        ("dry_run", False, False, False),
    ],
)
async def test_program_actions_match_mode_and_active_version(
    monkeypatch, mode, active_version, shows_stop, shows_resume
):
    async def current_user(_request):
        return _user()

    async def page_data(*_args, **_kwargs):
        data = _dashboard()
        data["settings"]["mode"] = mode
        if active_version:
            version = _version(status="active")
            data["versions"] = [version]
            data["settings"]["active_version_id"] = version["id"]
        return data

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "get_marketing_page_data", page_data)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/marketing/")

    assert response.status_code == 200
    assert ("Экстренная остановка" in response.text) is shows_stop
    assert ("Возобновить программу" in response.text) is shows_resume


@pytest.mark.asyncio
async def test_empty_dashboard_explains_next_step_without_fake_pagination(monkeypatch):
    async def current_user(_request):
        return _user()

    async def page_data(*_args, **_kwargs):
        return _dashboard()

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "get_marketing_page_data", page_data)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.get("/marketing/")

    assert response.status_code == 200
    assert "Нет клиентов с подтверждённым рекламным согласием" in response.text
    assert "Реактивации ещё не запускались" in response.text
    assert "Клиенты ещё не давали рекламное согласие" in response.text
    assert '<option value="90" selected>90 дней без визита или обращения</option>' in response.text
    assert "Страница 1" not in response.text


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
@pytest.mark.parametrize(
    "form",
    [
        {"main_text": " "},
        {"main_text": "x" * 4097},
        {"reminder_after_days": "5", "reminder_text": " "},
        {"reminder_after_days": "5", "reminder_text": "x" * 4097},
    ],
)
async def test_crafted_version_form_rejects_unusable_templates(
    monkeypatch, form
):
    async def current_user(_request):
        return _user()

    async def create_must_not_run(*_args, **_kwargs):
        raise AssertionError("invalid policy reached persistence")

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "create_draft", create_must_not_run)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/marketing/versions",
            data={"csrf_token": "known-csrf", **form},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["preview", "test", "activate"])
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("reactivation version not found"), 404),
        (ValueError("retired reactivation version cannot be used"), 409),
    ],
)
async def test_version_actions_translate_missing_and_retired_to_4xx(
    monkeypatch, operation, error, expected
):
    async def current_user(_request):
        return _user()

    async def fail(*_args, **_kwargs):
        raise error

    function_name = {
        "preview": "preview_version",
        "test": "queue_test_send",
        "activate": "activate_version",
    }[operation]
    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, function_name, fail)
    data = {"csrf_token": "known-csrf"}
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/marketing/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/{operation}",
            data=data,
        )

    assert response.status_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "gate",
    ["fresh_preview", "current_watermarks", "test_sent", "eligible_recipients", "same_checksum"],
)
async def test_activation_gate_failures_return_conflict(monkeypatch, gate):
    async def current_user(_request):
        return _user()

    async def fail(*_args, **_kwargs):
        raise ActivationBlocked(gate)

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "activate_version", fail)
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/marketing/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/activate",
            data={"csrf_token": "known-csrf"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == f"activation_blocked:{gate}"


@pytest.mark.asyncio
async def test_preview_samples_are_rendered_only_from_current_response(monkeypatch):
    captured = []
    preview_calls = []
    sample_calls = []

    async def current_user(_request):
        return _user()

    async def preview(*_args, **_kwargs):
        preview_calls.append(True)

    async def preview_samples(_database, version_id, *, actor_id):
        sample_calls.append((version_id, actor_id))
        return ("telegram:***6789",)

    async def page_data(*_args, **_kwargs):
        return _dashboard()

    def render(request, name, context):
        captured.append(context)
        return RedirectResponse("/captured", status_code=200)

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "preview_version", preview)
    monkeypatch.setattr(reactivation_routes.rdb, "preview_samples", preview_samples)
    monkeypatch.setattr(reactivation_routes.rdb, "get_marketing_page_data", page_data)
    monkeypatch.setattr(reactivation_routes.templates, "TemplateResponse", render)
    parent = FastAPI()
    parent.mount("/admin", _app())
    async with AsyncClient(
        transport=ASGITransport(app=parent), base_url="http://test"
    ) as client:
        posted = await client.post(
            "/admin/marketing/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/preview",
            data={"csrf_token": "known-csrf"},
            follow_redirects=False,
        )
        first_get = await client.get(posted.headers["location"])
        refreshed = await client.get(posted.headers["location"])

    assert posted.status_code == 303
    assert posted.headers["location"] == (
        "/admin/marketing/?preview=ready&preview_version="
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    assert "telegram" not in posted.headers["location"]
    assert first_get.status_code == 200
    assert first_get.headers["cache-control"] == "no-store"
    assert refreshed.status_code == 200
    assert preview_calls == [True]
    assert len(sample_calls) == 2
    assert captured[-1]["data"]["preview_samples"] == ["telegram:***6789"]


@pytest.mark.asyncio
async def test_pagination_links_are_canonical_under_admin_root(monkeypatch):
    consent_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def current_user(_request):
        return _user()

    async def page_data(*_args, **_kwargs):
        data = _dashboard()
        data["pagination"] = {"page": 2, "has_next": True}
        return data

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(reactivation_routes.rdb, "get_marketing_page_data", page_data)
    parent = FastAPI()
    parent.mount("/admin", _app())
    async with AsyncClient(
        transport=ASGITransport(app=parent), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/admin/marketing/?page=2&consent_id={consent_id}"
        )

    assert response.status_code == 200
    assert (
        f'href="/admin/marketing/?page=1&amp;consent_id={consent_id}"'
        in response.text
    )
    assert (
        f'href="/admin/marketing/?page=3&amp;consent_id={consent_id}"'
        in response.text
    )
    assert "/versions/" not in response.text.split("customer-events-pagination", 1)[1]


@pytest.mark.asyncio
async def test_activation_launches_without_typed_confirmation(monkeypatch):
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
        accepted = await client.post(
            "/marketing/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/activate",
            data={"csrf_token": "known-csrf"},
            follow_redirects=False,
        )

    assert accepted.status_code == 302
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] == UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert kwargs == {"actor_id": 7, "start_program": True}


@pytest.mark.asyncio
async def test_active_mode_does_not_require_typed_confirmation(monkeypatch):
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
        accepted = await client.post(
            "/marketing/mode",
            data={
                "csrf_token": "known-csrf",
                "mode": "active",
            },
            follow_redirects=False,
        )

    assert accepted.status_code == 302
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_consent_revoke_uses_only_opaque_consent_id(monkeypatch):
    consent_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    calls = []

    async def current_user(_request):
        return _user()

    async def revoke(_database, **values):
        calls.append(values)
        return {
            "id": values["consent_id"],
            "active": False,
            "consent_version": "marketing-v1",
        }

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", object)
    monkeypatch.setattr(
        reactivation_routes.rdb, "revoke_marketing_consent_by_id", revoke
    )
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
    assert calls[0]["consent_id"] == consent_id
    assert calls[0]["actor_id"] == 7
    assert calls[0]["ip_address"] == "127.0.0.1"
    assert calls[0]["user_agent"]
    assert "telegram" not in response.headers["location"]


@pytest.mark.asyncio
async def test_consent_revoke_resolves_identity_and_applies_both_events_in_one_transaction(
    monkeypatch,
):
    consent_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    transaction = SimpleTransaction()
    connection = SimpleConnection(consent_id, transaction)
    service = SimpleConsentService()
    audits = []
    monkeypatch.setattr(rdb, "ConsentService", lambda _database: service)
    async def audit(connection, **values):
        audits.append((connection, values))
    monkeypatch.setattr(rdb, "record_audit_in_transaction", audit)

    result = await rdb.revoke_marketing_consent_by_id(
        SimpleDatabase(connection), consent_id=consent_id, actor_id=7,
        ip_address="127.0.0.1", user_agent="test",
    )

    assert transaction.entered and transaction.exited
    assert connection.initial_lookup == consent_id
    assert connection.locked_subject == customer_lock_subject("42")
    assert [call[0] for call in service.calls] == ["revoke", "suppress"]
    assert all(call[1]["channel"] == "telegram" for call in service.calls)
    assert all(call[1]["user_id"] == "42" for call in service.calls)
    assert all(call[1]["connection"] is connection for call in service.calls)
    assert service.calls[1][1]["reason"] == "admin_revoke"
    assert audits[0][0] is connection
    assert audits[0][1]["object_id"] == str(consent_id)
    assert "user_id" not in repr(audits)
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
