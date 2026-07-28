import importlib.util
from pathlib import Path


def load_migration():
    path = Path("/workspace/migrations/versions/0009_production_admin.py")
    spec = importlib.util.spec_from_file_location("migration_0009", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_admin_migration_follows_0008():
    migration = load_migration()

    assert migration.revision == "0009_production_admin"
    assert migration.down_revision == "0008_yclients_lifecycle"


def test_admin_migration_creates_expected_tables():
    migration = load_migration()

    source = Path(migration.__file__).read_text(encoding="utf-8")
    assert "admin_users" in source
    assert "admin_sessions" in source
    assert "admin_audit_events" in source
