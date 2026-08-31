from pathlib import Path
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException

import auth
import reactivation_routes


ADMIN = Path("/workspace/admin")


def test_reactivation_tab_files_and_navigation_exist():
    routes = ADMIN / "reactivation_routes.py"
    template = ADMIN / "templates" / "reactivation.html"
    base = ADMIN / "templates" / "base.html"

    assert routes.exists()
    assert template.exists()
    assert "/reactivation/" in base.read_text(encoding="utf-8")
    html = template.read_text(encoding="utf-8")
    assert "Реактивация" in html
    assert "Отправка не подключена" in html
    assert "Записать согласие" in html
    assert "Создать черновик" in html
    assert "Поставить в очередь" in html
    assert "Журнал кампаний" in html
    assert "Отправить сообщения" not in html


def _user(role="owner"):
    return auth.AuthenticatedUser(
        id=7,
        username="owner",
        role=role,
        csrf_token="known-csrf",
        session_id="session",
    )


def _request():
    return SimpleNamespace(scope={"root_path": "/admin"}, client=None, headers={})


@pytest.mark.asyncio
async def test_page_is_owner_only_before_database_read(monkeypatch):
    async def current_user(_request):
        return _user("admin")

    def database_must_not_be_read():
        raise AssertionError("database read before RBAC")

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        reactivation_routes.database,
        "get_database",
        database_must_not_be_read,
    )

    with pytest.raises(HTTPException) as denied:
        await reactivation_routes.reactivation_page(_request())

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_settings_reject_bad_csrf_before_write(monkeypatch):
    async def current_user(_request):
        return _user()

    async def write_must_not_run(*_args, **_kwargs):
        raise AssertionError("settings write before CSRF")

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.rdb, "save_settings", write_must_not_run)

    with pytest.raises(HTTPException) as denied:
        await reactivation_routes.reactivation_settings(
            _request(),
            after_visit_days=1,
            sleeping_days=90,
            discount_percent=10,
            monthly_message_limit=1,
            ignore_limit=2,
            base_offer="Оффер",
            llm_instruction="Инструкция",
            csrf_token="wrong",
        )

    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_valid_settings_are_saved_and_audited(monkeypatch):
    saved = {}
    audits = []

    async def current_user(_request):
        return _user()

    async def get_settings(_database):
        return {
            "discount_percent": 0,
            "updated_at": datetime(2026, 8, 30, tzinfo=UTC),
        }

    async def save_settings(_database, **values):
        saved.update(values)
        return {
            **values,
            "updated_at": datetime(2026, 8, 30, tzinfo=UTC),
        }

    async def record_audit(**values):
        audits.append(values)

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", lambda: object())
    monkeypatch.setattr(reactivation_routes.rdb, "get_settings", get_settings)
    monkeypatch.setattr(reactivation_routes.rdb, "save_settings", save_settings)
    monkeypatch.setattr(reactivation_routes, "record_audit", record_audit)

    response = await reactivation_routes.reactivation_settings(
        _request(),
        after_visit_days=2,
        sleeping_days=120,
        discount_percent=15,
        monthly_message_limit=2,
        ignore_limit=3,
        base_offer="Оффер",
        llm_instruction="Инструкция",
        csrf_token="known-csrf",
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/reactivation/?saved=1"
    assert saved["discount_percent"] == 15
    assert audits[0]["action"] == "reactivation.settings_saved"
    json.dumps(audits[0]["before"])
    json.dumps(audits[0]["after"])


@pytest.mark.asyncio
async def test_consent_action_is_allowlisted_and_persisted(monkeypatch):
    calls = []
    audits = []

    async def current_user(_request):
        return _user()

    async def set_consent(_database, **values):
        calls.append(values)
        return {"id": UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), **values}

    async def capture_audit(**values):
        audits.append(values)

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", lambda: object())
    monkeypatch.setattr(reactivation_routes.rdb, "set_marketing_consent", set_consent)
    monkeypatch.setattr(reactivation_routes, "record_audit", capture_audit)

    with pytest.raises(HTTPException) as invalid:
        await reactivation_routes.reactivation_consent(
            _request(),
            channel="telegram",
            user_id="42",
            consent_version="marketing-v1",
            action="private-action",
            csrf_token="known-csrf",
        )
    with pytest.raises(HTTPException) as forbidden_grant:
        await reactivation_routes.reactivation_consent(
            _request(),
            channel="telegram",
            user_id="42",
            consent_version="marketing-v1",
            action="grant",
            csrf_token="known-csrf",
        )
    response = await reactivation_routes.reactivation_consent(
        _request(),
        channel="telegram",
        user_id="42",
        consent_version="marketing-v1",
        action="revoke",
        csrf_token="known-csrf",
    )

    assert invalid.value.status_code == 422
    assert forbidden_grant.value.status_code == 422
    assert response.headers["location"] == "/admin/reactivation/?consent=revoked"
    assert calls == [{
        "channel": "telegram",
        "user_id": "42",
        "consent_version": "marketing-v1",
        "active": False,
    }]
    assert audits[0]["object_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert "42" not in repr(audits[0])


@pytest.mark.asyncio
async def test_campaign_can_be_drafted_or_queued_without_sender(monkeypatch):
    queued = []
    campaign_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    async def current_user(_request):
        return _user()

    async def create_campaign(_database, **values):
        assert values == {"segment": "regular", "created_by": 7}
        return campaign_id

    async def queue_campaign(_database, **values):
        queued.append(values["campaign_id"])
        return {"recipient_count": 0}

    async def no_audit(**_values):
        return None

    monkeypatch.setattr(reactivation_routes, "get_current_user", current_user)
    monkeypatch.setattr(reactivation_routes.database, "get_database", lambda: object())
    monkeypatch.setattr(reactivation_routes.rdb, "create_campaign", create_campaign)
    monkeypatch.setattr(reactivation_routes.rdb, "queue_campaign", queue_campaign)
    monkeypatch.setattr(reactivation_routes, "record_audit", no_audit)

    response = await reactivation_routes.reactivation_campaign_create(
        _request(),
        segment="regular",
        action="queue",
        csrf_token="known-csrf",
    )

    assert response.headers["location"] == "/admin/reactivation/?campaign=queued"
    assert queued == [campaign_id]
    assert not hasattr(reactivation_routes, "send_campaign")
