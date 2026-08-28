import importlib
import os

import pytest

import config as llm_config
import eval_runner
from moroz.security.provider_config import resolve_provider_tuple


PROVIDER_ENV_KEYS = (
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "ROUTER_API_KEY",
    "ROUTER_BASE_URL",
    "ROUTER_MODEL",
    "COMPACT_API_KEY",
    "COMPACT_BASE_URL",
    "COMPACT_MODEL",
    "JUDGE_API_KEY",
    "JUDGE_BASE_URL",
    "JUDGE_MODEL",
)


def test_provider_tuple_is_inherited_atomically():
    parent = ("claude-sonnet-4", "primary-key", None)

    assert resolve_provider_tuple({}, "ROUTER", parent) == parent


def test_provider_tuple_treats_redundant_parent_values_as_inheritance():
    parent = ("gpt-4.1-mini", "primary-key", "https://primary.invalid/v1")

    assert resolve_provider_tuple(
        {
            "JUDGE_MODEL": "gpt-4.1-mini",
            "JUDGE_BASE_URL": "https://primary.invalid/v1",
        },
        "JUDGE",
        parent,
    ) == parent


@pytest.mark.parametrize("override", [{"ROUTER_MODEL": "gpt-4.1-mini"}, {"ROUTER_BASE_URL": "https://other.invalid/v1"}])
def test_provider_tuple_rejects_partial_override_without_dedicated_key(override):
    with pytest.raises(ValueError, match="ROUTER_API_KEY.*ROUTER_MODEL"):
        resolve_provider_tuple(override, "ROUTER", ("claude-sonnet-4", "primary-key", None))


def test_provider_tuple_accepts_complete_dedicated_provider():
    assert resolve_provider_tuple(
        {
            "ROUTER_MODEL": "gpt-4.1-mini",
            "ROUTER_API_KEY": "router-key",
            "ROUTER_BASE_URL": "https://router.invalid/v1",
        },
        "ROUTER",
        ("claude-sonnet-4", "primary-key", None),
    ) == ("gpt-4.1-mini", "router-key", "https://router.invalid/v1")


def test_runtime_and_eval_defaults_inherit_primary_provider_atomically(monkeypatch):
    original = {key: os.environ.get(key) for key in PROVIDER_ENV_KEYS}
    try:
        for key in PROVIDER_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4")
        monkeypatch.setenv("LLM_API_KEY", "primary-key")

        importlib.reload(llm_config)
        importlib.reload(eval_runner)

        primary = (
            llm_config.LLM_MODEL,
            llm_config.LLM_API_KEY,
            llm_config.LLM_BASE_URL,
        )
        assert (
            llm_config.ROUTER_MODEL,
            llm_config.ROUTER_API_KEY,
            llm_config.ROUTER_BASE_URL,
        ) == primary
        assert (
            llm_config.COMPACT_MODEL,
            llm_config.COMPACT_API_KEY,
            llm_config.COMPACT_BASE_URL,
        ) == primary
        assert (
            eval_runner.JUDGE_MODEL,
            eval_runner.JUDGE_API_KEY,
            eval_runner.JUDGE_BASE_URL,
        ) == primary
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(llm_config)
        importlib.reload(eval_runner)


def test_eval_clients_use_bounded_timeout(monkeypatch):
    created = []
    monkeypatch.setattr(eval_runner, "LLM_REQUEST_TIMEOUT_SEC", 17, raising=False)
    monkeypatch.setattr(
        eval_runner,
        "AsyncOpenAI",
        lambda **kwargs: created.append(kwargs) or kwargs,
    )

    eval_runner._create_client("test-key", None, "openai")

    assert created == [{"api_key": "test-key", "timeout": 17, "max_retries": 0}]
