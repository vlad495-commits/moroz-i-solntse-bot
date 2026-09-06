import logging
from types import SimpleNamespace

import pytest

import bot_control_routes
from auth import AuthenticatedUser
import llm as llm_module
import llm_status


class RedisOperationError(RuntimeError):
    pass


class FailingRedisClient:
    def __init__(self, *, fail_on="get"):
        self.fail_on = fail_on
        self.close_calls = 0

    async def get(self, _key):
        if self.fail_on == "get":
            raise RedisOperationError(
                "redis://user:password@redis operation-user-sentinel"
            )
        return None

    async def set(self, _key, _value):
        if self.fail_on == "set":
            raise RedisOperationError(
                "redis://user:password@redis toggle-user-sentinel"
            )

    async def delete(self, _key):
        return None

    async def publish(self, _channel, _value):
        raise RedisOperationError(
            "redis://user:password@redis prompt-payload-sentinel"
        )

    async def aclose(self):
        self.close_calls += 1


def admin_user() -> AuthenticatedUser:
    return AuthenticatedUser(
        id=7,
        username="admin",
        role="owner",
        csrf_token="csrf-token",
        session_id="session-id",
    )


async def current_admin_user(_request):
    return admin_user()


@pytest.mark.asyncio
async def test_llm_status_failure_is_redacted_and_client_closes_once(
    monkeypatch, caplog
):
    client = FailingRedisClient()
    url = "redis://user:password-sentinel@redis/0"
    monkeypatch.setattr(llm_status, "REDIS_URL", url)
    monkeypatch.setattr(
        llm_status.aioredis, "from_url", lambda *_args, **_kwargs: client
    )

    with caplog.at_level(logging.ERROR, logger=llm_status.logger.name):
        result = await llm_status.get_llm_status()

    assert result["main"] == {"status": "unknown"}
    assert result["reserve"] == {"status": "unknown"}
    assert client.close_calls == 1
    assert "llm_status_redis_failed error_type=RedisOperationError" in caplog.text
    assert "password-sentinel" not in caplog.text
    assert "operation-user-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_bot_control_page_uses_generic_error_and_closes_client(
    monkeypatch, caplog
):
    client = FailingRedisClient()
    monkeypatch.setattr(
        bot_control_routes,
        "get_current_user",
        current_admin_user,
    )

    async def redis_client():
        return client

    monkeypatch.setattr(bot_control_routes, "_redis_client", redis_client)
    monkeypatch.setattr(
        bot_control_routes.templates,
        "TemplateResponse",
        lambda _request, _name, context: context,
    )

    with caplog.at_level(logging.ERROR, logger=bot_control_routes.logger.name):
        context = await bot_control_routes.bot_control_page(object())

    assert context["error"] == "Сервис временно недоступен"
    assert client.close_calls == 1
    assert "bot_control_read_failed error_type=RedisOperationError" in caplog.text
    assert "operation-user-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_bot_control_toggle_failure_is_redacted_and_client_closes(
    monkeypatch, caplog
):
    client = FailingRedisClient(fail_on="set")
    monkeypatch.setattr(
        bot_control_routes,
        "get_current_user",
        current_admin_user,
    )

    async def redis_client():
        return client

    monkeypatch.setattr(bot_control_routes, "_redis_client", redis_client)

    with caplog.at_level(logging.ERROR, logger=bot_control_routes.logger.name):
        response = await bot_control_routes.bot_control_toggle(
            object(),
            csrf_token="csrf-token",
        )

    assert response.status_code == 302
    assert client.close_calls == 1
    assert "bot_control_toggle_failed error_type=RedisOperationError" in caplog.text
    assert "toggle-user-sentinel" not in caplog.text


def test_prompt_reload_rejects_invalid_facts_without_partial_state(
    monkeypatch, tmp_path
):
    prompt_path = tmp_path / "system.md"
    prompt_path.write_text("new prompt", encoding="utf-8")
    old_pipeline = SimpleNamespace(
        gateway=object(),
        system_prompt="old prompt",
        facts="old facts",
    )
    monkeypatch.setattr(llm_module, "SYSTEM_PROMPT_PATH", prompt_path)
    monkeypatch.setattr(llm_module, "_system_prompt", "old prompt")
    monkeypatch.setattr(llm_module, "_pipeline", old_pipeline)
    monkeypatch.setattr(
        llm_module,
        "extract_structured_facts",
        lambda _prompt: (_ for _ in ()).throw(ValueError("invalid prompt")),
    )

    with pytest.raises(ValueError, match="invalid prompt"):
        llm_module._load_prompt()

    assert llm_module._system_prompt == "old prompt"
    assert llm_module._pipeline is old_pipeline
    assert old_pipeline.system_prompt == "old prompt"
    assert old_pipeline.facts == "old facts"


def test_prompt_reload_rejects_missing_file_without_clearing_active_state(
    monkeypatch, tmp_path
):
    missing = tmp_path / "missing.md"
    old_pipeline = SimpleNamespace(
        gateway=object(),
        system_prompt="old prompt",
        facts="old facts",
    )
    monkeypatch.setattr(llm_module, "SYSTEM_PROMPT_PATH", missing)
    monkeypatch.setattr(llm_module, "_system_prompt", "old prompt")
    monkeypatch.setattr(llm_module, "_pipeline", old_pipeline)

    with pytest.raises(FileNotFoundError):
        llm_module._load_prompt()

    assert llm_module._system_prompt == "old prompt"
    assert llm_module._pipeline is old_pipeline


def test_init_llm_does_not_log_raw_custom_base_url(monkeypatch, caplog):
    base_url = "https://user:password-sentinel@provider.test/v1?token=secret"
    monkeypatch.setattr(llm_module, "LLM_API_KEY", "configured")
    monkeypatch.setattr(llm_module, "LLM_BASE_URL", base_url)
    monkeypatch.setattr(llm_module, "LLM_MODEL", "safe-model")
    monkeypatch.setattr(llm_module, "_system_prompt", "safe prompt")
    monkeypatch.setattr(llm_module, "_load_prompt", lambda: None)
    monkeypatch.setattr(llm_module, "_create_client", lambda *_args: object())

    with caplog.at_level(logging.INFO, logger=llm_module.logger.name):
        llm_module.init_llm()

    assert "llm_client_created" in caplog.text
    assert "kind=openai" in caplog.text
    assert "model=safe-model" in caplog.text
    assert "custom_endpoint=True" in caplog.text
    assert base_url not in caplog.text
    assert "password-sentinel" not in caplog.text
