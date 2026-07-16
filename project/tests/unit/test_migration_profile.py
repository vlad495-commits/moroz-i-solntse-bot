from pathlib import Path
import re

import yaml


ROOT = Path("/workspace")


def compose_services():
    return yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )["services"]


def test_migration_commands_are_excluded_from_default_profile():
    services = compose_services()

    assert services["migrate"]["profiles"] == ["migration"]
    assert services["cutover"]["profiles"] == ["migration"]


def test_migration_commands_share_immutable_image_without_bind_mounts():
    services = compose_services()

    assert services["migrate"]["image"] == services["cutover"]["image"]
    assert services["migrate"]["image"] == (
        "${MIGRATION_IMAGE:-moroz-i-solntse-migrate:local}"
    )
    for name in ("migrate", "cutover"):
        assert services[name]["build"] == {
            "context": ".",
            "dockerfile": "migrate/Dockerfile",
        }
        assert "volumes" not in services[name]

    assert services["migrate"]["command"] == [
        "alembic",
        "-c",
        "/app/alembic.ini",
        "upgrade",
        "head",
    ]
    assert services["cutover"]["command"] == [
        "python",
        "/app/migrations/audit_existing_schema.py",
        "--config",
        "/app/alembic.ini",
    ]


def test_migration_image_is_minimal_and_non_root():
    dockerfile = (ROOT / "migrate/Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "migrate/requirements.txt").read_text(encoding="utf-8")

    user_directives = [
        line.strip() for line in dockerfile.splitlines()
        if line.strip().startswith("USER ")
    ]
    assert user_directives[-1] == "USER appuser"
    assert "COPY migrate/requirements.txt" in dockerfile
    assert "COPY alembic.ini" in dockerfile
    assert "COPY migrations" in dockerfile
    assert "requirements-dev" not in dockerfile
    assert "COPY src" not in dockerfile
    assert requirements.splitlines() == [
        "alembic==1.18.5",
        "SQLAlchemy==2.0.51",
        "asyncpg==0.31.0",
    ]


def test_build_context_excludes_sensitive_and_runtime_artifacts():
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".env",
        ".env.*",
        "data/",
        "logs/",
        "tmp/",
        "__pycache__/",
        "*.py[cod]",
        ".pytest_cache/",
        ".coverage",
        "htmlcov/",
    } <= patterns


def test_test_image_does_not_copy_scheduler_runtime():
    dockerfile = (ROOT / "Dockerfile.test").read_text(encoding="utf-8")

    assert "COPY scheduler" not in dockerfile


def test_compose_process_environment_overrides_external_test_credentials():
    services = compose_services()

    assert services["postgres"]["environment"] == {
        "POSTGRES_USER": "${POSTGRES_USER:?set POSTGRES_USER outside Git}",
        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD outside Git}",
        "POSTGRES_DB": "${POSTGRES_DB:?set POSTGRES_DB outside Git}",
    }
    assert services["redis"]["environment"] == {
        "REDIS_PASSWORD": "${REDIS_PASSWORD:?set REDIS_PASSWORD outside Git}",
    }
    database_keys = {
        "DATABASE_URL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    }
    allowed = {
        "test": database_keys | {"RABBITMQ_URL", "REDIS_URL"},
        "migrate": database_keys,
        "cutover": database_keys,
    }
    for name, keys in allowed.items():
        assert "env_file" not in services[name]
        assert set(services[name]["environment"]) == keys
        assert services[name]["environment"]["DATABASE_URL"] == (
            "${DATABASE_URL:-}"
        )
        for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            assert services[name]["environment"][key] == (
                f"${{{key}:?set {key} outside Git}}"
            )
    assert services["admin"]["environment"] == {
        "DATABASE_URL": "${DATABASE_URL:-}",
        "REDIS_URL": "${REDIS_URL:?set REDIS_URL outside Git}",
    }
    assert services["bot"]["environment"] == {
        "DATABASE_URL": "${DATABASE_URL:-}",
        "REDIS_URL": "${REDIS_URL:?set REDIS_URL outside Git}",
        "TELEGRAM_WEBHOOK_SECRET": (
            "${TELEGRAM_WEBHOOK_SECRET:?set TELEGRAM_WEBHOOK_SECRET outside Git}"
        ),
    }
    for name in ("bot", "worker"):
        assert "redis" not in services[name]["depends_on"]

    assert services["worker"]["environment"] == {
        "RABBITMQ_URL": "${RABBITMQ_URL:?set RABBITMQ_URL outside Git}",
        "DATABASE_URL": "${DATABASE_URL:-}",
        "POSTGRES_USER": "${POSTGRES_USER:?set POSTGRES_USER outside Git}",
        "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD outside Git}",
        "POSTGRES_DB": "${POSTGRES_DB:?set POSTGRES_DB outside Git}",
        "REDIS_URL": "${REDIS_URL:?set REDIS_URL outside Git}",
        "TELEGRAM_BOT_TOKEN": "${TELEGRAM_BOT_TOKEN:-}",
        "LLM_API_KEY": "${LLM_API_KEY:-}",
        "OPENAI_API_KEY": "${OPENAI_API_KEY:-}",
        "LLM_BASE_URL": "${LLM_BASE_URL:-}",
        "LLM_MODEL": "${LLM_MODEL:-gpt-4.1-mini}",
        "LLM_TEMPERATURE": "${LLM_TEMPERATURE:-0.3}",
        "LLM_MAX_TOKENS": "${LLM_MAX_TOKENS:-2000}",
        "LLM_REQUEST_TIMEOUT_SEC": "${LLM_REQUEST_TIMEOUT_SEC:-30}",
        "CONTEXT_MESSAGES_LIMIT": "${CONTEXT_MESSAGES_LIMIT:-20}",
    }
    for name in ("worker", "redis", "postgres"):
        assert "env_file" not in services[name]


def test_worker_image_installs_only_exact_pipeline_dependencies():
    dockerfile = (ROOT / "worker/Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "worker/requirements.txt").read_text(encoding="utf-8")

    assert requirements.splitlines() == [
        "aio-pika==9.6.2",
        "aiogram==3.27.0",
        "anthropic==0.116.0",
        "asyncpg==0.31.0",
        "openai==2.33.0",
        "python-dotenv==1.2.2",
        "redis[hiredis]==7.4.0",
    ]
    assert "COPY worker/requirements.txt" in dockerfile
    assert "llm/requirements.txt" not in dockerfile
    assert "COPY llm/llm.py llm/config.py" in dockerfile


def test_all_python_services_pin_same_native_anthropic_runtime():
    bot = (ROOT / "llm/requirements.txt").read_text(encoding="utf-8")
    worker = (ROOT / "worker/requirements.txt").read_text(encoding="utf-8")
    admin = (ROOT / "admin/requirements.txt").read_text(encoding="utf-8")

    assert "anthropic==0.116.0" in bot.splitlines()
    assert "anthropic==0.116.0" in worker.splitlines()
    assert "anthropic==0.116.0" in admin.splitlines()


def test_worker_and_scheduler_healthchecks_require_fresh_runtime_signals():
    services = compose_services()
    worker_health = " ".join(services["worker"]["healthcheck"]["test"])
    scheduler_health = " ".join(services["scheduler"]["healthcheck"]["test"])

    assert "/proc/1/cmdline" in worker_health
    assert "/tmp/worker-ready" in worker_health
    assert "/proc/1/cmdline" in scheduler_health
    assert "/tmp/scheduler-heartbeat" in scheduler_health
    assert "75" in scheduler_health


def test_admin_port_is_isolatable_without_changing_default_url():
    assert compose_services()["admin"]["ports"] == [
        "${ADMIN_PORT:-8080}:8080"
    ]


def test_host_ops_regression_checks_rendered_compose_environment_allowlists():
    script = (ROOT / "tests/ops/verify_compose_db_fallback.ps1").read_text(
        encoding="utf-8"
    )

    assert "config --format json" in script
    assert '$expectedEnvironment = @{' in script
    worker_literal = re.search(r"worker = @\(([^)]*)\)", script)
    assert worker_literal
    scripted_worker_keys = set(re.findall(r'"([A-Z_]+)"', worker_literal.group(1)))
    assert scripted_worker_keys == set(compose_services()["worker"]["environment"])
    assert 'redis = @("REDIS_PASSWORD")' in script
    assert 'postgres = @("POSTGRES_DB", "POSTGRES_PASSWORD", "POSTGRES_USER")' in script
    assert 'env_file' in script
