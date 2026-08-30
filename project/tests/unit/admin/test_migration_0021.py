from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "0021_reactivation_draft.py"
)


def test_reactivation_draft_migration_extends_only_delivery_status():
    text = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "0021_reactivation_draft"' in text
    assert 'down_revision = "0020_admin_reactivation"' in text
    assert "'draft', 'queued', 'skipped', 'sent', 'error'" in text
    assert "reactivation_deliveries" in text
