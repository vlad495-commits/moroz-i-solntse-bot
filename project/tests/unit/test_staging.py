import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path("/workspace")
BASE = ROOT / "docker-compose.yml"
STAGING = ROOT / "docker-compose.staging.yml"
CADDYFILE = ROOT / "ops/staging/Caddyfile"


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor(
    "!reset", lambda loader, node: loader.construct_sequence(node)
)


def load_compose(path):
    return yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)


def load_staging():
    return load_compose(STAGING)


def load_staging_module():
    spec = importlib.util.spec_from_file_location(
        "staging_ops", ROOT / "ops/staging.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def webhook_env(**overrides):
    values = {
        "TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyzABCDE",
        "STAGING_TELEGRAM_BOT_ID": "123456",
        "STAGING_PUBLIC_URL": "https://staging.example.test",
        "TELEGRAM_WEBHOOK_SECRET": "A" * 32,
    }
    values.update(overrides)
    return values


def test_staging_override_tags_apps_and_never_publishes_stores():
    services = load_staging()["services"]
    assert services["bot"]["image"] == "moroz-staging-bot:${STAGING_IMAGE_TAG:?set STAGING_IMAGE_TAG}"
    assert services["worker"]["image"] == "moroz-staging-worker:${STAGING_IMAGE_TAG:?set STAGING_IMAGE_TAG}"
    assert services["migrate"]["image"] == "moroz-staging-migrate:${STAGING_IMAGE_TAG:?set STAGING_IMAGE_TAG}"
    assert services["cutover"]["image"] == services["migrate"]["image"]
    assert services["bot"]["ports"] == [
        "127.0.0.1:${STAGING_BOT_PORT:-18081}:8081"
    ]
    for name in ("postgres", "redis", "rabbitmq"):
        assert "ports" not in services.get(name, {})


def test_caddy_is_pinned_non_root_and_routes_only_webhook():
    compose = load_staging()
    caddy = compose["services"]["caddy"]
    assert caddy["image"] == "caddy:2.11.4"
    assert caddy["profiles"] == ["staging-ingress"]
    assert caddy["user"] == "10001:10001"
    assert caddy["read_only"] is True
    assert caddy["cap_drop"] == ["ALL"]
    assert caddy["security_opt"] == ["no-new-privileges:true"]
    assert caddy["ports"] == ["80:8080", "443:8443", "443:8443/udp"]
    assert set(compose["volumes"]) == {
        "staging_caddy_data",
        "staging_caddy_config",
    }

    text = CADDYFILE.read_text(encoding="utf-8")
    assert "http_port 8080" in text
    assert "https_port 8443" in text
    assert "path /telegram/webhook" in text
    assert "reverse_proxy bot:8081" in text
    assert "respond 404" in text
    assert "/admin" not in text
    assert "/openapi.json" not in text


def test_staging_bot_healthcheck_probes_http_listener():
    health = " ".join(load_staging()["services"]["bot"]["healthcheck"]["test"])
    assert "http://127.0.0.1:8081/openapi.json" in health
    assert "/proc/1/cmdline" not in health


def test_caddy_runs_capability_free_copy_under_no_new_privileges():
    caddy = load_staging()["services"]["caddy"]
    entrypoint = " ".join(caddy["entrypoint"])
    assert "cp /usr/bin/caddy /tmp/caddy" in entrypoint
    assert 'exec /tmp/caddy "$$@"' in entrypoint
    assert caddy["tmpfs"] == ["/tmp:exec"]
    assert caddy["healthcheck"]["test"] == [
        "CMD",
        "/tmp/caddy",
        "validate",
        "--config",
        "/etc/caddy/Caddyfile",
    ]


def test_caddy_volumes_preserve_image_writable_storage_directories():
    volumes = load_staging()["services"]["caddy"]["volumes"]
    assert "staging_caddy_data:/data/caddy" in volumes
    assert "staging_caddy_config:/config/caddy" in volumes
    assert "staging_caddy_data:/data" not in volumes
    assert "staging_caddy_config:/config" not in volumes


def test_merged_staging_disables_admin_and_scheduler_and_resets_admin_ports():
    base = load_compose(BASE)["services"]
    override = load_staging()["services"]
    merged = {
        name: base.get(name, {}) | override.get(name, {})
        for name in base.keys() | override.keys()
    }

    assert merged["admin"]["profiles"] == ["disabled-in-staging"]
    assert merged["scheduler"]["profiles"] == ["disabled-in-staging"]
    assert merged["admin"]["ports"] == []


def test_staging_webhook_receives_only_required_environment():
    webhook = load_staging()["services"]["staging-webhook"]
    assert "env_file" not in webhook
    assert set(webhook["environment"]) == {
        "TELEGRAM_BOT_TOKEN",
        "STAGING_TELEGRAM_BOT_ID",
        "STAGING_PUBLIC_URL",
        "TELEGRAM_WEBHOOK_SECRET",
    }


def test_webhook_config_rejects_http_and_invalid_secret():
    staging = load_staging_module()
    with pytest.raises(ValueError, match="staging_public_url_invalid"):
        staging.WebhookConfig.from_env(
            webhook_env(STAGING_PUBLIC_URL="http://staging.example.test")
        )
    with pytest.raises(ValueError, match="webhook_secret_invalid"):
        staging.WebhookConfig.from_env(
            webhook_env(TELEGRAM_WEBHOOK_SECRET="secret with spaces")
        )


@pytest.mark.asyncio
async def test_identity_mismatch_stops_before_set_webhook():
    staging = load_staging_module()

    class FakeBot:
        def __init__(self, token):
            self.set_calls = []
            self.session = SimpleNamespace(close=self.close)

        async def close(self):
            return None

        async def get_me(self):
            return SimpleNamespace(id=999999)

        async def set_webhook(self, **kwargs):
            self.set_calls.append(kwargs)

    created = []
    with pytest.raises(RuntimeError, match="staging_bot_identity_mismatch"):
        await staging.manage_webhook(
            "set",
            staging.WebhookConfig.from_env(webhook_env()),
            bot_factory=lambda token: created.append(FakeBot(token)) or created[-1],
        )
    assert created[0].set_calls == []


@pytest.mark.asyncio
async def test_set_webhook_uses_exact_safe_contract():
    staging = load_staging_module()

    class FakeBot:
        def __init__(self, token):
            self.set_calls = []
            self.session = SimpleNamespace(close=self.close)

        async def close(self):
            return None

        async def get_me(self):
            return SimpleNamespace(id=123456)

        async def set_webhook(self, **kwargs):
            self.set_calls.append(kwargs)
            return True

    bot = FakeBot("unused")
    result = await staging.manage_webhook(
        "set",
        staging.WebhookConfig.from_env(webhook_env()),
        bot_factory=lambda _token: bot,
    )
    assert result == {"ok": True, "action": "set"}
    assert bot.set_calls == [{
        "url": "https://staging.example.test/telegram/webhook",
        "secret_token": "A" * 32,
        "allowed_updates": ["message", "callback_query"],
        "max_connections": 5,
        "drop_pending_updates": False,
    }]


def test_cli_failure_prints_only_error_type(monkeypatch, capsys):
    staging = load_staging_module()
    sentinel = "https://user:password@provider.test private-user-text"

    async def fail(_args):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(staging, "run", fail)
    monkeypatch.setattr(
        staging,
        "build_parser",
        lambda: SimpleNamespace(parse_args=lambda: SimpleNamespace()),
    )
    assert staging.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"ok": False, "error_type": "RuntimeError"}
    assert sentinel not in output
