import importlib
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient


auth = importlib.import_module("auth")
admin_app = importlib.import_module("app")


@pytest.mark.asyncio
async def test_chat_detail_renders_message_usage_states_and_groups(monkeypatch):
    async def current_user(_request):
        return auth.AuthenticatedUser(
            id=7,
            username="admin",
            role="admin",
            csrf_token="csrf-token",
            session_id="session-id",
        )

    created_at = datetime(2026, 8, 30, 14, 0, tzinfo=UTC)
    detail = {
        "chat_id": 42,
        "user_id": 7,
        "username": "client",
        "stats": {},
        "messages": [
            {
                "id": 1,
                "role": "user",
                "content": "old",
                "created_at": created_at,
                "llm_usage_state": "unavailable",
                "usage_groups": [],
            },
            {
                "id": 2,
                "role": "user",
                "content": "without llm",
                "created_at": created_at,
                "llm_usage_state": "none",
                "usage_groups": [],
            },
            {
                "id": 3,
                "role": "user",
                "content": "with llm",
                "created_at": created_at,
                "llm_usage_state": "used",
                "usage_groups": [
                    {
                        "purpose": "router",
                        "model": "gpt-4o-mini",
                        "llm_calls": 2,
                        "prompt_tokens": 1000,
                        "completion_tokens": 100,
                        "cached_tokens": 200,
                        "total_tokens": 1100,
                    },
                    {
                        "purpose": "answer",
                        "model": "gpt-4.1",
                        "llm_calls": 1,
                        "prompt_tokens": 500,
                        "completion_tokens": 50,
                        "cached_tokens": 100,
                        "total_tokens": 550,
                    },
                    {
                        "purpose": "<script>unknown</script>",
                        "model": "safe-model",
                        "llm_calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "cached_tokens": 0,
                        "total_tokens": 0,
                    },
                ],
            },
            {
                "id": 4,
                "role": "assistant",
                "content": "answer",
                "created_at": created_at,
                "llm_usage_state": "unavailable",
                "usage_groups": [],
            },
        ],
    }

    async def get_detail(_chat_id):
        return detail

    async def get_events(*_args, **_kwargs):
        return {"items": [], "next_cursor": None, "has_more": False}

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(admin_app, "get_current_user", current_user)
    monkeypatch.setattr(admin_app.database, "get_chat_detail", get_detail)
    monkeypatch.setattr(admin_app.database, "get_customer_events", get_events)
    monkeypatch.setattr(admin_app, "record_audit", no_audit)

    async with AsyncClient(
        transport=ASGITransport(app=admin_app.app),
        base_url="http://test",
    ) as client:
        response = await client.get("/chats/42")

    assert response.status_code == 200
    assert response.text.count('class="message-llm-analytics') == 3
    assert "Нет точной привязки" in response.text
    assert "LLM не вызывалась" in response.text
    assert "Итого: 3 LLM-вызова" in response.text
    assert "1 650 токенов" in response.text
    assert "Prompt: 1 500" in response.text
    assert "Completion: 150" in response.text
    assert "Кэш: 300" in response.text
    assert "Router" in response.text
    assert "Answer" in response.text
    assert "Compact" not in response.text
    assert "gpt-4o-mini" in response.text
    assert "gpt-4.1" in response.text
    assert "Стоимость:" in response.text
    assert "Сэкономлено:" in response.text
    assert "<script>unknown</script>" not in response.text
    assert "&lt;script&gt;unknown&lt;/script&gt;" in response.text
    assistant_html = response.text.split("answer", 1)[1]
    assert 'class="message-llm-analytics' not in assistant_html
