import importlib

import pytest
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
admin_app = importlib.import_module("app")


@pytest.mark.asyncio
async def test_chats_page_renders_actual_model_matrix(monkeypatch):
    async def current_user(_request):
        return auth.AuthenticatedUser(
            id=7,
            username="admin",
            role="admin",
            csrf_token="csrf-token",
            session_id="session-id",
        )

    async def no_chats(*_args, **_kwargs):
        return []

    async def no_total(*_args, **_kwargs):
        return 0

    async def empty_stats(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(admin_app, "get_current_user", current_user)
    monkeypatch.setattr(admin_app.database, "get_chats_list", no_chats)
    monkeypatch.setattr(admin_app.database, "get_chats_total", no_total)
    monkeypatch.setattr(admin_app.database, "get_global_stats", empty_stats)
    monkeypatch.setenv("LLM_MODEL", "answer-main")
    monkeypatch.setenv("RESERVE_MODEL", "answer-reserve")
    monkeypatch.setenv("ROUTER_MODEL", "router-main")
    monkeypatch.setenv("SECURITY_MODEL", "security-main")
    monkeypatch.setenv("OUTPUT_VALIDATOR_ENABLED", "true")

    async with AsyncClient(
        transport=ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "LLM-модели по модулям" in response.text
    for title in (
        "Основной ответ",
        "Роутер",
        "LLM Security",
        "Валидация",
        "Контекст",
    ):
        assert title in response.text
    assert response.text.count("answer-main") == 2
    assert response.text.count("answer-reserve") == 3
    assert response.text.count("router-main") == 2
    assert response.text.count("security-main") == 1
    assert response.text.count("не предусмотрена") == 2
    assert "включена" in response.text
    assert 'class="stat stat-models dialogs-model-card"' in response.text
