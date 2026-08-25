import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import llm as llm_module


@pytest.mark.asyncio
async def test_advertised_native_claude_provider_can_create_client():
    from anthropic import AsyncAnthropic

    client = llm_module._create_client("test-api-key", None, "anthropic")
    try:
        assert isinstance(client, AsyncAnthropic)
    finally:
        await client.close()


def test_init_llm_builds_a_dedicated_toolless_router_provider(monkeypatch, tmp_path):
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
    monkeypatch.setattr(llm_module, "RESERVE_API_KEY", "")
    monkeypatch.setattr(llm_module, "RESERVE_MODEL", "")
    monkeypatch.setattr(llm_module, "ROUTER_API_KEY", "router-key", raising=False)
    monkeypatch.setattr(
        llm_module,
        "ROUTER_BASE_URL",
        "https://router.invalid/v1",
        raising=False,
    )
    monkeypatch.setattr(llm_module, "ROUTER_MODEL", "router-model", raising=False)
    monkeypatch.setattr(llm_module, "ROUTER_MAX_TOKENS", 120, raising=False)
    monkeypatch.setattr(llm_module, "_create_client", create_client)
    monkeypatch.setattr(llm_module, "_primary_client", None)
    monkeypatch.setattr(llm_module, "_pipeline_client", None)
    monkeypatch.setattr(llm_module, "_pipeline", None)

    llm_module.init_llm()

    assert [(item.api_key, item.base_url) for item in clients] == [
        ("answer-key", "https://answer.invalid/v1"),
        ("router-key", "https://router.invalid/v1"),
    ]
    answer_provider = llm_module._pipeline.gateway.primary
    router_provider = llm_module._pipeline.router._provider
    assert answer_provider.client is clients[0]
    assert router_provider.client is clients[1]
    assert router_provider.model == "router-model"
    assert router_provider.temperature == 0.0
    assert router_provider.max_tokens == 120
    assert llm_module._pipeline.router is not answer_provider


def test_prompt_reload_preserves_the_configured_router_instance(monkeypatch, tmp_path):
    prompt_path = tmp_path / "system.md"
    prompt_path.write_text("new safe prompt", encoding="utf-8")
    router = object()
    gateway = object()
    monkeypatch.setattr(llm_module, "SYSTEM_PROMPT_PATH", prompt_path)
    monkeypatch.setattr(
        llm_module,
        "_pipeline",
        SimpleNamespace(gateway=gateway, router=router),
    )

    llm_module._load_prompt()

    assert llm_module._pipeline.gateway is gateway
    assert llm_module._pipeline.router is router


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
