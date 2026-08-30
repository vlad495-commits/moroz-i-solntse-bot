import importlib

import pytest
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
admin_app = importlib.import_module("app")
prompt_routes = importlib.import_module("prompt_routes")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tab", "title", "marker"),
    (
        ("router", "Промпт роутера", "Выбери ровно один маршрут"),
        ("security", "Промпт LLM Security", "Верни строго одно слово"),
        ("validator", "Промпт валидации ответа", "Validate the candidate reply"),
        ("compact", "Промпт контекста", "Кратко сожми старую часть"),
    ),
)
async def test_prompt_auxiliary_tabs_render_runtime_prompts_read_only(
    monkeypatch,
    tab,
    title,
    marker,
):
    async def current_user(_request):
        return auth.AuthenticatedUser(
            id=1,
            username="owner",
            role="owner",
            csrf_token="csrf-token",
            session_id="session-id",
        )

    async def versions_must_not_be_read(*_args, **_kwargs):
        raise AssertionError("auxiliary prompt must not query main prompt history")

    monkeypatch.setattr(prompt_routes, "get_current_user", current_user)
    monkeypatch.setattr(prompt_routes.pdb, "list_versions", versions_must_not_be_read)

    async with AsyncClient(
        transport=ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        response = await client.get(f"/prompt/?tab={tab}")

    assert response.status_code == 200
    assert title in response.text
    assert marker in response.text
    assert "Только просмотр" in response.text
    assert "readonly" in response.text
    assert "Сохранить и применить" not in response.text
    for label in (
        "Основной промпт",
        "Роутер",
        "LLM Security",
        "Валидация",
        "Контекст",
    ):
        assert label in response.text
