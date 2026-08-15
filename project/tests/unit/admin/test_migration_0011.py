import importlib.util
from pathlib import Path


def load_migration():
    path = Path("/workspace/migrations/versions/0011_yclients_service_catalog.py")
    spec = importlib.util.spec_from_file_location("migration_0011", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalog_migration_is_one_bounded_table():
    migration = load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert migration.revision == "0011_yclients_service_catalog"
    assert migration.down_revision == "0010_yclients_projection"
    assert source.count("op.create_table(") == 1
    assert '"yclients_service_catalog"' in source
    assert "numeric(10, 2)" in source.casefold()
    assert "duration_minutes" in source
    assert "raw" not in source.casefold()
    assert "payload" not in source.casefold()
