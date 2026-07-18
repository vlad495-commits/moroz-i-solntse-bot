from pathlib import Path

import yaml


ROOT = Path("/workspace")
STAGING = ROOT / "docker-compose.staging.yml"
CADDYFILE = ROOT / "ops/staging/Caddyfile"


def load_staging():
    return yaml.safe_load(STAGING.read_text(encoding="utf-8"))


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
