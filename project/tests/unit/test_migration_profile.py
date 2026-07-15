from pathlib import Path

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
    allowed = {
        "test": {"DATABASE_URL", "RABBITMQ_URL"},
        "migrate": {"DATABASE_URL"},
        "cutover": {"DATABASE_URL"},
    }
    for name, keys in allowed.items():
        assert "env_file" not in services[name]
        assert set(services[name]["environment"]) == keys
    for name in ("bot", "admin"):
        assert services[name]["environment"] == {
            "DATABASE_URL": "${DATABASE_URL:?set DATABASE_URL outside Git}",
            "REDIS_URL": "${REDIS_URL:?set REDIS_URL outside Git}",
        }


def test_admin_port_is_isolatable_without_changing_default_url():
    assert compose_services()["admin"]["ports"] == [
        "${ADMIN_PORT:-8080}:8080"
    ]
