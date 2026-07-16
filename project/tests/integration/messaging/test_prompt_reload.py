import asyncio
import os
from types import SimpleNamespace

import pytest
import redis.asyncio as redis

import llm as llm_module
from config import PROMPT_RELOAD_CHANNEL


pytestmark = pytest.mark.asyncio


async def test_reload_listener_changes_prompt_used_by_generate_response(
    monkeypatch, tmp_path
):
    prompt_path = tmp_path / "system.md"
    prompt_path.write_text("Первый prompt", encoding="utf-8")
    monkeypatch.setattr(llm_module, "SYSTEM_PROMPT_PATH", prompt_path)
    monkeypatch.setattr(llm_module, "_primary_client", object())
    captured = []

    async def invoke(messages):
        captured.append(messages)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            ),
            model="fake",
        )

    monkeypatch.setattr(llm_module, "_invoke", invoke)
    llm_module._load_prompt()
    await llm_module.generate_response("Вопрос", [])

    client = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    listener = asyncio.create_task(llm_module.prompt_reload_listener())
    try:
        for _ in range(100):
            subscriptions = await client.pubsub_numsub(PROMPT_RELOAD_CHANNEL)
            if subscriptions[0][1] == 1:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("prompt listener did not subscribe")

        prompt_path.write_text("Второй prompt", encoding="utf-8")
        await client.publish(PROMPT_RELOAD_CHANNEL, "reload")
        for _ in range(100):
            if llm_module._system_prompt == "Второй prompt":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("prompt listener did not reload")
        await llm_module.generate_response("Вопрос", [])
    finally:
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)
        await client.aclose()

    assert captured[0][0] == {"role": "system", "content": "Первый prompt"}
    assert captured[1][0] == {"role": "system", "content": "Второй prompt"}
