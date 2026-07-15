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


class PrimaryProviderError(RuntimeError):
    pass


class ReserveProviderError(RuntimeError):
    pass


class EvalCaseError(RuntimeError):
    pass


class EvalRunError(RuntimeError):
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


@pytest.mark.asyncio
async def test_primary_llm_failure_before_reserve_logs_only_error_type(
    monkeypatch, caplog
):
    primary = object()
    reserve = object()
    sentinel = "https://user:password@provider.test exception-user-sentinel"

    async def invoke(client, *_args, **_kwargs):
        if client is primary:
            raise PrimaryProviderError(sentinel)
        return "safe reserve response"

    monkeypatch.setattr(eval_runner, "_primary", primary)
    monkeypatch.setattr(eval_runner, "_reserve", reserve)
    monkeypatch.setattr(eval_runner, "_invoke_llm", invoke)

    with caplog.at_level(logging.WARNING, logger=eval_runner.logger.name):
        response = await eval_runner._generate_bot_response(
            "private-question-sentinel", ""
        )

    assert response == "safe reserve response"
    assert "primary_llm_failed" in caplog.text
    assert "error_type=PrimaryProviderError" in caplog.text
    assert sentinel not in caplog.text
    assert "private-question-sentinel" not in caplog.text


@pytest.mark.asyncio
async def test_reserve_llm_failure_logs_only_error_type(monkeypatch, caplog):
    primary = object()
    reserve = object()
    primary_sentinel = "https://primary:password@provider.test primary-user-text"
    reserve_sentinel = "https://reserve:password@provider.test reserve-user-text"

    async def invoke(client, *_args, **_kwargs):
        if client is primary:
            raise PrimaryProviderError(primary_sentinel)
        raise ReserveProviderError(reserve_sentinel)

    monkeypatch.setattr(eval_runner, "_primary", primary)
    monkeypatch.setattr(eval_runner, "_reserve", reserve)
    monkeypatch.setattr(eval_runner, "_invoke_llm", invoke)

    with caplog.at_level(logging.WARNING, logger=eval_runner.logger.name):
        with pytest.raises(PrimaryProviderError):
            await eval_runner._generate_bot_response("safe question", "")

    assert "primary_llm_failed" in caplog.text
    assert "error_type=PrimaryProviderError" in caplog.text
    assert "reserve_llm_failed" in caplog.text
    assert "error_type=ReserveProviderError" in caplog.text
    assert primary_sentinel not in caplog.text
    assert reserve_sentinel not in caplog.text


@pytest.mark.asyncio
async def test_run_case_sanitizes_log_and_database_error(monkeypatch, caplog):
    sentinel = "https://case:password@provider.test case-user-text"
    saved = {}

    async def fail_response(*_args, **_kwargs):
        raise EvalCaseError(sentinel)

    async def save_result(**kwargs):
        saved.update(kwargs)
        return 91

    monkeypatch.setattr(eval_runner, "_read_system_prompt", lambda: "")
    monkeypatch.setattr(eval_runner, "_generate_bot_response", fail_response)
    monkeypatch.setattr(eval_runner.evdb, "save_result", save_result)

    case = {"id": 17, "question": "safe question", "expected_answer": "safe"}
    with caplog.at_level(logging.ERROR, logger=eval_runner.logger.name):
        result = await eval_runner.run_case(case, run_id=23)

    assert result["verdict"] == "error"
    assert saved["error_message"] == "EvalCaseError"
    assert "eval_case_failed case_id=17 error_type=EvalCaseError" in caplog.text
    assert sentinel not in caplog.text
    assert sentinel not in repr(saved)


@pytest.mark.asyncio
async def test_run_eval_set_sanitizes_log_and_database_error(monkeypatch, caplog):
    sentinel = "https://run:password@provider.test run-user-text"
    finished = []

    async def fail_case(*_args, **_kwargs):
        raise EvalRunError(sentinel)

    async def finish_run(*args, **kwargs):
        finished.append((args, kwargs))

    monkeypatch.setattr(eval_runner, "_init_clients", lambda: None)
    monkeypatch.setattr(eval_runner, "run_case", fail_case)
    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish_run)

    with caplog.at_level(logging.ERROR, logger=eval_runner.logger.name):
        await eval_runner.run_eval_set(29, cases=[{"id": 31}])

    assert finished == [
        ((29, 0, 0), {"status": "error", "error_message": "EvalRunError"})
    ]
    assert "eval_run_failed run_id=29 error_type=EvalRunError" in caplog.text
    assert sentinel not in caplog.text
    assert sentinel not in repr(finished)


def test_invalid_regex_log_does_not_include_pattern(monkeypatch, caplog):
    pattern = "r:[regex-user-sentinel"

    with caplog.at_level(logging.WARNING, logger=eval_runner.logger.name):
        assert not eval_runner._matches_keyword("safe text", pattern)

    assert "invalid_eval_regex" in caplog.text
    assert f"pattern_length={len(pattern) - 2}" in caplog.text
    assert pattern not in caplog.text
