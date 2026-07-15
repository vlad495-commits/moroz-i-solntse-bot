import asyncio
import logging

import pytest

import bot_control_routes
import llm as llm_module
import llm_status
import prompt_routes


class RedisOperationError(RuntimeError):
    pass


class PromptListenerError(RuntimeError):
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
    monkeypatch.setattr(bot_control_routes, "get_current_user", lambda _request: "admin")

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
    monkeypatch.setattr(bot_control_routes, "get_current_user", lambda _request: "admin")

    async def redis_client():
        return client

    monkeypatch.setattr(bot_control_routes, "_redis_client", redis_client)

    with caplog.at_level(logging.ERROR, logger=bot_control_routes.logger.name):
        response = await bot_control_routes.bot_control_toggle(object())

    assert response.status_code == 302
    assert client.close_calls == 1
    assert "bot_control_toggle_failed error_type=RedisOperationError" in caplog.text
    assert "toggle-user-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_prompt_reload_publish_failure_is_redacted_and_client_closes(
    monkeypatch, caplog
):
    client = FailingRedisClient(fail_on="publish")
    monkeypatch.setattr(
        prompt_routes.aioredis, "from_url", lambda *_args, **_kwargs: client
    )

    with caplog.at_level(logging.ERROR, logger=prompt_routes.logger.name):
        await prompt_routes._publish_reload(41)

    assert client.close_calls == 1
    assert "prompt_reload_publish_failed error_type=RedisOperationError" in caplog.text
    assert "prompt-payload-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_prompt_listener_failure_is_redacted_and_resources_close(
    monkeypatch, caplog
):
    class FailingPubSub:
        def __init__(self):
            self.close_calls = 0

        async def subscribe(self, _channel):
            return None

        async def listen(self):
            raise PromptListenerError(
                "redis://user:password@redis reload-payload-sentinel"
            )
            yield

        async def aclose(self):
            self.close_calls += 1

    class ListenerClient:
        def __init__(self, pubsub):
            self._pubsub = pubsub
            self.close_calls = 0

        def pubsub(self):
            return self._pubsub

        async def aclose(self):
            self.close_calls += 1

    pubsub = FailingPubSub()
    client = ListenerClient(pubsub)
    monkeypatch.setattr(
        llm_module.aioredis, "from_url", lambda *_args, **_kwargs: client
    )

    async def stop_after_failure(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(llm_module.asyncio, "sleep", stop_after_failure)

    with caplog.at_level(logging.ERROR, logger=llm_module.logger.name):
        with pytest.raises(asyncio.CancelledError):
            await llm_module.prompt_reload_listener()

    assert pubsub.close_calls == 1
    assert client.close_calls == 1
    assert "prompt_reload_listener_failed error_type=PromptListenerError" in caplog.text
    assert "reload-payload-sentinel" not in caplog.text


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
