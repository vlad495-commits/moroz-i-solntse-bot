import logging
from types import SimpleNamespace

import pytest

import cache as llm_cache
import eval_runner


class HealthyRedis:
    async def ping(self):
        return True

    async def aclose(self):
        return None


class SentinelRedisError(RuntimeError):
    pass


class FailingRedis:
    async def ping(self):
        raise SentinelRedisError("redis-exception-sentinel")


@pytest.mark.asyncio
async def test_cache_logs_connection_without_url_or_raw_exception(monkeypatch, caplog):
    redis_url = "redis://user:password-sentinel@redis:6379/0"
    healthy = HealthyRedis()
    monkeypatch.setattr(llm_cache, "REDIS_URL", redis_url)
    monkeypatch.setattr(llm_cache.aioredis, "from_url", lambda *_args, **_kwargs: healthy)
    monkeypatch.setattr(llm_cache, "_redis", None)

    with caplog.at_level(logging.INFO, logger=llm_cache.logger.name):
        await llm_cache.init_cache()

    assert "redis_connected" in caplog.text
    assert redis_url not in caplog.text
    assert "password-sentinel" not in caplog.text

    caplog.clear()
    monkeypatch.setattr(llm_cache, "_redis", FailingRedis())
    monkeypatch.setattr(
        llm_cache.aioredis,
        "from_url",
        lambda *_args, **_kwargs: FailingRedis(),
    )

    with caplog.at_level(logging.WARNING, logger=llm_cache.logger.name):
        assert not await llm_cache._ensure_redis()

    assert "redis_connection_lost" in caplog.text
    assert "redis_unavailable" in caplog.text
    assert "SentinelRedisError" in caplog.text
    assert "redis-exception-sentinel" not in caplog.text
    assert redis_url not in caplog.text


@pytest.mark.asyncio
async def test_judge_invalid_json_log_does_not_include_raw_content(monkeypatch, caplog):
    content = "judge-content-sentinel with private question and answer"

    async def create(**_kwargs):
        message = SimpleNamespace(content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    judge = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(eval_runner, "_judge", judge)
    monkeypatch.setattr(eval_runner, "_judge_kind", "openai")

    with caplog.at_level(logging.WARNING, logger=eval_runner.logger.name):
        score, reasoning = await eval_runner.llm_judge(
            "private question", "private answer", "actual answer"
        )

    assert score == 0.0
    assert reasoning == "Judge parse error"
    assert "judge_invalid_json" in caplog.text
    assert f"content_length={len(content)}" in caplog.text
    assert content not in caplog.text
    assert "private question" not in caplog.text
    assert "private answer" not in caplog.text
