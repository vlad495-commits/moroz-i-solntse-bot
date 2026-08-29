import importlib.util
from pathlib import Path


MIGRATION = Path("/workspace/migrations/versions/0018_simple_security_pipeline.py")


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0018", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migration_updates_only_six_false_positive_sources(monkeypatch):
    migration = _load_migration()
    statements = []
    monkeypatch.setattr(migration.op, "execute", statements.append)

    migration.upgrade()

    assert len(statements) == 1
    statement = statements[0]
    assert "jsonb_set(expected_data, '{source}', '\"llm\"')" in statement
    assert statement.count("security-fp-") == 6
    assert "WHERE suite = 'security'" in statement
