import importlib.util
from pathlib import Path


MODULE_PATH = Path("/workspace/migrations/database_url.py")


def load_module():
    spec = importlib.util.spec_from_file_location("migration_database_url", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_database_url_encodes_reserved_parts():
    module = load_module()
    env = {
        "POSTGRES_USER": "app@team",
        "POSTGRES_PASSWORD": "p@:/%",
        "POSTGRES_DB": "moroz/db",
    }

    assert module.database_url(env) == (
        "postgresql://app%40team:p%40%3A%2F%25@postgres:5432/moroz%2Fdb"
    )
    assert module.sqlalchemy_database_url(env) == (
        "postgresql+asyncpg://app%40team:p%40%3A%2F%25@postgres:5432/moroz%2Fdb"
    )


def test_migration_explicit_database_url_is_unchanged_before_driver_conversion():
    module = load_module()
    explicit = "postgresql://literal%2Fuser:literal%25pass@db:5432/name?x=%2F"

    assert module.database_url({"DATABASE_URL": explicit}) == explicit
