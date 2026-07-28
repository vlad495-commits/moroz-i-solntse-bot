import importlib.util
from pathlib import Path


PROJECT_ROOT = Path("/workspace")
if not PROJECT_ROOT.exists():
    PROJECT_ROOT = Path(__file__).resolve().parents[3]


def load_validator():
    path = PROJECT_ROOT / "ops" / "validate_env.py"
    spec = importlib.util.spec_from_file_location("validate_env", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_env():
    return {
        "PUBLIC_DOMAIN": "bot.example.ru",
        "PUBLIC_BASE_URL": "https://bot.example.ru",
        "POSTGRES_USER": "moroz",
        "POSTGRES_PASSWORD": "postgres-secret",
        "POSTGRES_DB": "moroz",
        "REDIS_PASSWORD": "redis-secret",
        "REDIS_URL": "redis://:redis-secret@redis:6379/0",
        "RABBITMQ_USER": "moroz",
        "RABBITMQ_PASSWORD": "rabbit-secret",
        "RABBITMQ_URL": "amqp://moroz:rabbit-secret@rabbitmq:5672/",
        "TELEGRAM_BOT_TOKEN": "123456:telegram-token",
        "TELEGRAM_WEBHOOK_SECRET": "webhook-secret-value",
        "ADMIN_USERNAME": "owner",
        "ADMIN_PASSWORD": "owner-password-value",
        "ADMIN_SESSION_SECRET": "session-secret-value-min-32-characters",
        "ADMIN_COOKIE_SECURE": "true",
        "YCLIENTS_PARTNER_TOKEN": "partner-token",
        "YCLIENTS_USER_TOKEN": "user-token",
        "YCLIENTS_COMPANY_ID": "12345",
        "YCLIENTS_BASE_URL": "https://api.yclients.com",
        "BACKUP_ENCRYPTION_KEY": "backup-secret-value-min-32-characters",
    }


def test_validate_env_accepts_required_production_values():
    validator = load_validator()

    assert validator.validate(valid_env()) == []


def test_validate_env_rejects_default_admin_credentials_and_short_secret():
    validator = load_validator()
    env = valid_env()
    env.update(
        {
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "admin",
            "ADMIN_SESSION_SECRET": "short",
        }
    )

    errors = validator.validate(env)

    assert "ADMIN_USERNAME must not be the default admin user" in errors
    assert "ADMIN_PASSWORD must not be the default admin password" in errors
    assert "ADMIN_SESSION_SECRET must be at least 32 characters" in errors


def test_validate_env_requires_secure_admin_cookie():
    validator = load_validator()
    env = valid_env()
    env["ADMIN_COOKIE_SECURE"] = "false"

    errors = validator.validate(env)

    assert "ADMIN_COOKIE_SECURE must be true in production" in errors


def test_validate_env_rejects_missing_webhook_yclients_and_http_public_url():
    validator = load_validator()
    env = valid_env()
    env.update(
        {
            "PUBLIC_BASE_URL": "http://bot.example.ru",
            "TELEGRAM_WEBHOOK_SECRET": "",
            "YCLIENTS_PARTNER_TOKEN": "",
            "BACKUP_ENCRYPTION_KEY": "",
        }
    )

    errors = validator.validate(env)

    assert "PUBLIC_BASE_URL must start with https://" in errors
    assert "TELEGRAM_WEBHOOK_SECRET is required" in errors
    assert "YCLIENTS_PARTNER_TOKEN is required" in errors
    assert "BACKUP_ENCRYPTION_KEY is required" in errors


def test_validate_env_rejects_placeholders_short_secrets_and_domain_mismatch():
    validator = load_validator()
    env = valid_env()
    env.update(
        {
            "PUBLIC_DOMAIN": "bot.example.ru",
            "PUBLIC_BASE_URL": "https://other.example.ru",
            "TELEGRAM_BOT_TOKEN": "replace-with-telegram-bot-token",
            "TELEGRAM_WEBHOOK_SECRET": "short",
            "ADMIN_PASSWORD": "replace-with-strong-admin-password",
            "BACKUP_ENCRYPTION_KEY": "replace-with-at-least-32-random-characters",
        }
    )

    errors = validator.validate(env)

    assert "PUBLIC_BASE_URL host must match PUBLIC_DOMAIN" in errors
    assert "TELEGRAM_BOT_TOKEN must not use a placeholder value" in errors
    assert "TELEGRAM_WEBHOOK_SECRET must be at least 16 characters" in errors
    assert "ADMIN_PASSWORD must not use a placeholder value" in errors
    assert "BACKUP_ENCRYPTION_KEY must not use a placeholder value" in errors


def test_production_compose_adds_caddy_and_keeps_admin_localhost_only():
    compose = (PROJECT_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "caddy:" in compose
    assert "caddy:2.10-alpine" in compose
    assert "80:80" in compose
    assert "443:443" in compose
    assert "ports: !override" in compose
    assert "127.0.0.1:${ADMIN_PORT:-8080}:8080" in compose
    assert "ops/Caddyfile:/etc/caddy/Caddyfile:ro" in compose
    assert "./ops:/ops:ro" in compose
    assert "pgbackups:/backups/postgres" in compose
    assert "ops-check:" in compose
    assert "BACKUP_ENCRYPTION_KEY: ${BACKUP_ENCRYPTION_KEY:?set BACKUP_ENCRYPTION_KEY outside Git}" in compose
    assert "ADMIN_ROOT_PATH: /admin" in compose
    assert "ADMIN_COOKIE_SECURE: ${ADMIN_COOKIE_SECURE:?set ADMIN_COOKIE_SECURE outside Git}" in compose


def test_caddyfile_routes_only_webhook_and_admin_prefix():
    caddyfile = (PROJECT_ROOT / "ops" / "Caddyfile").read_text(encoding="utf-8")

    assert "{$PUBLIC_DOMAIN}" in caddyfile
    assert "reverse_proxy bot:8081" in caddyfile
    assert "handle_path /admin/*" in caddyfile
    assert "reverse_proxy admin:8080" in caddyfile
    assert "respond 404" in caddyfile
