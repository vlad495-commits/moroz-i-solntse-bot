import importlib.util
from pathlib import Path


MIGRATION = Path(
    "/workspace/migrations/versions/0012_yclients_projection_suppression.py"
)


def load_migration():
    spec = importlib.util.spec_from_file_location("migration_0012", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_suppression_migration_is_metadata_only():
    migration = load_migration()
    source = MIGRATION.read_text(encoding="utf-8")

    assert migration.revision == "0012_projection_suppression"
    assert migration.down_revision == "0011_yclients_service_catalog"
    assert source.count("op.create_table(") == 1
    assert '"yclients_projection_suppressions"' in source
    assert 'sa.Column("external_id", sa.Text(), primary_key=True)' in source
    assert '"created_at"' in source
    assert "sa.DateTime(timezone=True)" in source
    for forbidden in (
        "chat_id",
        "client_name",
        "phone",
        "booking_key",
        "service_names",
    ):
        assert forbidden not in source
