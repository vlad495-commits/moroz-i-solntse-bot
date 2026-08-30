import importlib
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from auth import AuthenticatedUser


statistics_routes = importlib.import_module("statistics_routes")


def user(role="owner", csrf_token="known-csrf"):
    return AuthenticatedUser(
        id=7,
        username=role,
        role=role,
        csrf_token=csrf_token,
        session_id="session-id",
    )


def _test_app():
    app = FastAPI()
    app.include_router(statistics_routes.router)
    return app


@pytest.mark.asyncio
async def test_statistics_rejects_admin_before_database_read(monkeypatch):
    async def current_user(_request):
        return user(role="admin")

    async def unexpected_read(*_args):
        raise AssertionError("statistics must enforce RBAC before database reads")

    monkeypatch.setattr(statistics_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        statistics_routes.database,
        "get_statistics_snapshot",
        unexpected_read,
    )

    async with AsyncClient(
        transport=ASGITransport(app=_test_app()), base_url="http://test"
    ) as client:
        response = await client.get("/stats")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_statistics_rejects_reversed_period(monkeypatch):
    async def current_user(_request):
        return user()

    monkeypatch.setattr(statistics_routes, "get_current_user", current_user)

    async with AsyncClient(
        transport=ASGITransport(app=_test_app()), base_url="http://test"
    ) as client:
        response = await client.get("/stats?start=2026-08-31&end=2026-08-01")

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_statistics_settings_reject_bad_csrf_before_write(monkeypatch):
    async def current_user(_request):
        return user()

    async def unexpected_write(*_args):
        raise AssertionError("settings must enforce CSRF before database writes")

    monkeypatch.setattr(statistics_routes, "get_current_user", current_user)
    monkeypatch.setattr(
        statistics_routes.database,
        "save_statistics_settings",
        unexpected_write,
    )

    async with AsyncClient(
        transport=ASGITransport(app=_test_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/stats/settings",
            data={
                "csrf_token": "wrong",
                "minutes_per_dialogue": "20",
                "hourly_rate_rub": "600",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_owner_can_save_statistics_settings_with_audit(monkeypatch):
    saved = []
    audits = []

    async def current_user(_request):
        return user()

    async def save(minutes, rate):
        saved.append((minutes, rate))
        return {
            "minutes_per_dialogue": Decimal("20.00"),
            "hourly_rate_rub": Decimal("600.00"),
        }

    async def audit(**values):
        audits.append(values)

    monkeypatch.setattr(statistics_routes, "get_current_user", current_user)
    monkeypatch.setattr(statistics_routes.database, "save_statistics_settings", save)
    monkeypatch.setattr(statistics_routes, "record_audit", audit)

    async with AsyncClient(
        transport=ASGITransport(app=_test_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.post(
            "/stats/settings",
            data={
                "csrf_token": "known-csrf",
                "minutes_per_dialogue": "20",
                "hourly_rate_rub": "600",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/stats"
    assert saved == [(Decimal("20"), Decimal("600"))]
    assert audits[0]["action"] == "statistics.settings_updated"
    assert audits[0]["after"] == {
        "minutes_per_dialogue": "20.00",
        "hourly_rate_rub": "600.00",
    }


@pytest.mark.asyncio
async def test_statistics_page_is_transparent_about_estimates_and_missing_data(
    monkeypatch,
):
    async def current_user(_request):
        return user()

    async def snapshot(_period):
        return {
            "users": 2,
            "messages": 5,
            "automatic_replies": 3,
            "automated_dialogues": 1,
            "escalations": 1,
            "llm_calls": 2,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "cached_tokens": 20,
            "total_tokens": 150,
            "usage_rows": [
                {
                    "model": "gpt-5.6-luna",
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "cached_tokens": 20,
                }
            ],
            "security_incidents": None,
            "security_incidents_reason": (
                "Нет данных: Security-инциденты ещё не сохраняются."
            ),
        }

    async def settings():
        return {"minutes_per_dialogue": None, "hourly_rate_rub": None}

    monkeypatch.setattr(statistics_routes, "get_current_user", current_user)
    monkeypatch.setattr(statistics_routes.database, "get_statistics_snapshot", snapshot)
    monkeypatch.setattr(statistics_routes.database, "get_statistics_settings", settings)

    async with AsyncClient(
        transport=ASGITransport(app=_test_app()), base_url="http://test"
    ) as client:
        response = await client.get("/stats?start=2026-08-01&end=2026-08-31")

    assert response.status_code == 200
    assert 'name="start" value="2026-08-01"' in response.text
    assert 'name="end" value="2026-08-31"' in response.text
    assert 'name="csrf_token" value="known-csrf"' in response.text
    assert 'name="minutes_per_dialogue"' in response.text
    assert "Автоматизированные диалоги" in response.text
    assert "Расчётная оценка" in response.text
    assert "Это не доказательство полного решения обращения." in response.text
    assert (
        "сэкономленные часы = автоматизированные диалоги × минуты оператора / 60"
        in response.text
    )
    assert "экономия = сэкономленные часы × ставка оператора" in response.text
    assert "Нет данных: заполните минуты оператора и ставку." in response.text
    assert "Нет данных: для модели gpt-5.6-luna не задан тариф." in response.text
    assert "Нет данных: Security-инциденты ещё не сохраняются." in response.text
