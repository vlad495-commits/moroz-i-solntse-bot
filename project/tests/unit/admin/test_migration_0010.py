import importlib.util
from pathlib import Path


def load_migration():
    path = Path("/workspace/migrations/versions/0010_yclients_booking_projection.py")
    spec = importlib.util.spec_from_file_location("migration_0010", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_projection_migration_contract():
    migration = load_migration()
    source = Path(migration.__file__).read_text(encoding="utf-8")

    assert migration.revision == "0010_yclients_projection"
    assert migration.down_revision == "0009_production_admin"
    assert source.count("op.create_table(") == 1
    assert '"yclients_booking_projection"' in source
    assert '"ix_yclients_projection_starts_external"' in source
    assert '"ix_yclients_projection_booking_key"' in source
