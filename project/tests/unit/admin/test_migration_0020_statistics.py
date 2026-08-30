from pathlib import Path


MIGRATION = Path("/workspace/migrations/versions/0020_admin_statistics.py")


def test_statistics_migration_is_additive_and_singleton_scoped():
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "0020_admin_statistics"' in text
    assert 'down_revision = "0019_router_v2"' in text
    assert '"admin_statistics_settings"' in text
    assert "minutes_per_dialogue > 0" in text
    assert "hourly_rate_rub > 0" in text
    assert "ck_admin_statistics_settings_singleton" in text
    assert "op.drop_table(\"admin_statistics_settings\")" in text


def test_statistics_migration_adds_period_indexes():
    text = MIGRATION.read_text(encoding="utf-8")

    for name in (
        "ix_messages_created_at",
        "ix_token_usage_created_at",
        "ix_outbound_messages_created_at",
        "ix_escalations_created_at",
    ):
        assert name in text
