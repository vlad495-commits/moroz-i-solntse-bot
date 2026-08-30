from pathlib import Path


MIGRATION = Path(
    "/workspace/migrations/versions/0023_reactivation_draft.py"
)


def test_reactivation_draft_migration_follows_integrated_statistics_head():
    text = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "0023_reactivation_draft"' in text
    assert 'down_revision = "0022_admin_statistics"' in text
    assert "'draft', 'queued', 'skipped', 'sent', 'error'" in text
    assert "reactivation_deliveries" in text
