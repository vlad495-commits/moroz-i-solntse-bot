import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import config as llm_config
import llm as llm_module


@pytest.mark.asyncio
async def test_advertised_native_claude_provider_can_create_client():
    from anthropic import AsyncAnthropic

    client = llm_module._create_client("test-api-key", None, "anthropic")
    try:
        assert isinstance(client, AsyncAnthropic)
    finally:
        await client.close()


def test_init_llm_shares_router_client_with_compactor(
    monkeypatch,
    tmp_path,
):
    prompt_path = tmp_path / "system.md"
    prompt_path.write_text("safe owned prompt", encoding="utf-8")
    clients = []

    def create_client(api_key, base_url, kind):
        client = SimpleNamespace(
            api_key=api_key,
            base_url=base_url,
            kind=kind,
            index=len(clients),
        )
        clients.append(client)
        return client

    monkeypatch.setattr(llm_module, "SYSTEM_PROMPT_PATH", prompt_path)
    monkeypatch.setattr(llm_module, "LLM_API_KEY", "answer-key")
    monkeypatch.setattr(llm_module, "LLM_BASE_URL", "https://answer.invalid/v1")
    monkeypatch.setattr(llm_module, "LLM_MODEL", "answer-model")
    monkeypatch.setattr(llm_module, "RESERVE_API_KEY", "reserve-key")
    monkeypatch.setattr(llm_module, "RESERVE_BASE_URL", "https://reserve.invalid/v1")
    monkeypatch.setattr(llm_module, "RESERVE_MODEL", "reserve-model")
    monkeypatch.setattr(llm_module, "ROUTER_API_KEY", "router-key", raising=False)
    monkeypatch.setattr(
        llm_module,
        "ROUTER_BASE_URL",
        "https://router.invalid/v1",
        raising=False,
    )
    monkeypatch.setattr(llm_module, "ROUTER_MODEL", "router-model", raising=False)
    monkeypatch.setattr(llm_module, "ROUTER_MAX_TOKENS", 120, raising=False)
    monkeypatch.setattr(llm_module, "SECURITY_API_KEY", "security-key", raising=False)
    monkeypatch.setattr(
        llm_module,
        "SECURITY_BASE_URL",
        "https://security.invalid/v1",
        raising=False,
    )
    monkeypatch.setattr(llm_module, "SECURITY_MODEL", "security-model", raising=False)
    monkeypatch.setattr(llm_module, "SECURITY_MAX_TOKENS", 10, raising=False)
    monkeypatch.setattr(llm_module, "OUTPUT_VALIDATOR_ENABLED", True, raising=False)
    monkeypatch.setattr(llm_module, "COMPACT_MAX_TOKENS", 400, raising=False)
    monkeypatch.setattr(llm_module, "_create_client", create_client)
    monkeypatch.setattr(llm_module, "_primary_client", None)
    monkeypatch.setattr(llm_module, "_pipeline_client", None)
    monkeypatch.setattr(llm_module, "_pipeline", None)

    security_alert = object()
    output_alert = object()
    compact_alert = object()
    llm_module.init_llm(security_alert, output_alert, compact_alert)

    assert [(item.api_key, item.base_url) for item in clients] == [
        ("answer-key", "https://answer.invalid/v1"),
        ("reserve-key", "https://reserve.invalid/v1"),
        ("router-key", "https://router.invalid/v1"),
        ("security-key", "https://security.invalid/v1"),
    ]
    answer_provider = llm_module._pipeline.gateway.primary
    router_provider = llm_module._pipeline.router._provider
    security_provider = llm_module._pipeline.input_security._primary
    compact_provider = llm_module._pipeline.context_compactor._provider
    assert answer_provider.client is clients[0]
    assert router_provider.client is clients[2]
    assert router_provider.model == "router-model"
    assert router_provider.temperature == 0.0
    assert router_provider.max_tokens == 120
    assert llm_module._pipeline.router is not answer_provider
    assert security_provider.client is clients[3]
    assert security_provider.model == "security-model"
    assert security_provider.temperature == 0.0
    assert security_provider.max_tokens == 10
    assert llm_module._pipeline.input_security._reserve.client is clients[1]
    assert compact_provider.client is clients[2]
    assert compact_provider.model == "router-model"
    assert compact_provider.temperature == 0.0
    assert compact_provider.max_tokens == 400
    assert llm_module._pipeline.context_compactor._alert is compact_alert
    assert llm_module._pipeline.input_security._alert is security_alert
    assert llm_module._pipeline.output_validator._provider is llm_module._pipeline.gateway
    assert llm_module._pipeline.output_validator._alert is output_alert


def test_init_llm_disables_semantic_output_validator_by_default(monkeypatch, tmp_path):
    prompt_path = tmp_path / "system.md"
    prompt_path.write_text("safe owned prompt", encoding="utf-8")
    monkeypatch.setattr(llm_module, "SYSTEM_PROMPT_PATH", prompt_path)
    monkeypatch.setattr(llm_module, "LLM_API_KEY", "answer-key")
    monkeypatch.setattr(llm_module, "RESERVE_API_KEY", "")
    monkeypatch.setattr(llm_module, "RESERVE_MODEL", "")
    monkeypatch.setattr(llm_module, "OUTPUT_VALIDATOR_ENABLED", False, raising=False)
    monkeypatch.setattr(llm_module, "_create_client", lambda *_args: object())

    llm_module.init_llm()

    assert llm_module._pipeline.output_validator is None


def test_prompt_reload_preserves_configured_classifier_instances(monkeypatch, tmp_path):
    prompt_path = tmp_path / "system.md"
    prompt_path.write_text("new safe prompt", encoding="utf-8")
    router = object()
    input_security = object()
    output_validator = object()
    context_compactor = object()
    gateway = object()
    monkeypatch.setattr(llm_module, "SYSTEM_PROMPT_PATH", prompt_path)
    monkeypatch.setattr(
        llm_module,
        "_pipeline",
        SimpleNamespace(
            gateway=gateway,
            router=router,
            input_security=input_security,
            output_validator=output_validator,
            context_compactor=context_compactor,
        ),
    )

    llm_module._load_prompt()

    assert llm_module._pipeline.gateway is gateway
    assert llm_module._pipeline.router is router
    assert llm_module._pipeline.input_security is input_security
    assert llm_module._pipeline.output_validator is output_validator
    assert llm_module._pipeline.context_compactor is context_compactor


def _service_block(compose: str, service: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  \S[^\n]*:\n|\Z)",
        compose,
    )
    assert match is not None
    return match.group(0)


def test_router_environment_is_scoped_to_worker_and_admin_only():
    compose = Path("/workspace/docker-compose.yml").read_text(encoding="utf-8")
    variables = {
        "ROUTER_MODEL",
        "ROUTER_API_KEY",
        "ROUTER_BASE_URL",
        "ROUTER_MAX_TOKENS",
    }

    for service in ("worker", "admin"):
        block = _service_block(compose, service)
        assert all(f"      {variable}:" in block for variable in variables)

    for service in (
        "test",
        "migrate",
        "cutover",
        "scheduler",
        "bot",
        "redis",
        "postgres",
        "rabbitmq",
    ):
        block = _service_block(compose, service)
        assert all(f"      {variable}:" not in block for variable in variables)


def test_security_environment_is_scoped_to_worker_and_admin_only():
    compose = Path("/workspace/docker-compose.yml").read_text(encoding="utf-8")
    variables = {
        "SECURITY_MODEL",
        "SECURITY_API_KEY",
        "SECURITY_BASE_URL",
        "SECURITY_MAX_TOKENS",
    }

    for service in ("worker", "admin"):
        block = _service_block(compose, service)
        assert all(f"      {variable}:" in block for variable in variables)

    for service in (
        "test", "migrate", "cutover", "scheduler", "bot", "redis",
        "postgres", "rabbitmq",
    ):
        block = _service_block(compose, service)
        assert all(f"      {variable}:" not in block for variable in variables)


def test_output_validator_flag_is_scoped_to_worker_and_admin():
    compose = Path("/workspace/docker-compose.yml").read_text(encoding="utf-8")

    for service in ("worker", "admin"):
        assert "      OUTPUT_VALIDATOR_ENABLED:" in _service_block(compose, service)
    for service in (
        "test", "migrate", "cutover", "scheduler", "bot",
        "redis", "postgres", "rabbitmq",
    ):
        assert "      OUTPUT_VALIDATOR_ENABLED:" not in _service_block(compose, service)


def test_compact_uses_router_provider_and_scopes_only_runtime_limits():
    compose = Path("/workspace/docker-compose.yml").read_text(encoding="utf-8")
    removed = {
        "COMPACT_MODEL",
        "COMPACT_API_KEY",
        "COMPACT_BASE_URL",
    }
    variables = {
        "COMPACT_MAX_TOKENS",
        "COMPACT_THRESHOLD",
        "COMPACT_KEEP_RECENT",
    }

    assert all(f"      {variable}:" not in compose for variable in removed)

    for service in ("worker", "admin"):
        block = _service_block(compose, service)
        assert all(f"      {variable}:" in block for variable in variables)

    for service in (
        "test",
        "migrate",
        "cutover",
        "scheduler",
        "bot",
        "redis",
        "postgres",
        "rabbitmq",
    ):
        block = _service_block(compose, service)
        assert all(f"      {variable}:" not in block for variable in variables)


@pytest.mark.parametrize(
    "context_limit,threshold,keep_recent",
    [(30, 30, 10), (40, 30, 31), (40, 0, 0), (40, 30, 0)],
)
def test_invalid_compact_limits_are_rejected(
    context_limit,
    threshold,
    keep_recent,
):
    with pytest.raises(ValueError, match="invalid compact context limits"):
        llm_config._validate_context_limits(
            context_limit,
            threshold,
            keep_recent,
        )


def test_default_compact_limits_are_valid():
    assert (
        llm_config.CONTEXT_MESSAGES_LIMIT,
        llm_config.COMPACT_THRESHOLD,
        llm_config.COMPACT_KEEP_RECENT,
    ) == (40, 30, 10)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("false", False), ("TRUE", True), ("1", True)],
)
def test_catalog_grounding_flag_is_strict_and_defaults_off(value, expected):
    assert llm_config._parse_boolean(value, default=False) is expected


def test_catalog_grounding_flag_rejects_unknown_value():
    with pytest.raises(ValueError, match="invalid boolean setting"):
        llm_config._parse_boolean("sometimes", default=False)
