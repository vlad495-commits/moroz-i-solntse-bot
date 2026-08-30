from pathlib import Path


MIGRATION = Path("/workspace/migrations/versions/0020_admin_reactivation.py")


def test_reactivation_migration_owns_only_its_additive_tables():
    assert MIGRATION.exists(), "reactivation migration must exist"
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "0020_admin_reactivation"' in text
    assert 'down_revision = "0019_router_v2"' in text
    for table in (
        "marketing_consents",
        "reactivation_settings",
        "reactivation_campaigns",
        "reactivation_deliveries",
    ):
        assert f'"{table}"' in text
        assert f'op.drop_table("{table}")' in text
    assert "processing_consents" not in text
    assert "outbound_messages" not in text
    assert "task_outbox" not in text
    assert "scheduler_jobs" not in text


def test_reactivation_migration_has_fail_closed_constraints():
    assert MIGRATION.exists(), "reactivation migration must exist"
    text = MIGRATION.read_text(encoding="utf-8")

    assert "uq_marketing_consents_channel_user" in text
    assert "ck_reactivation_campaign_segment" in text
    assert "ck_reactivation_campaign_status" in text
    assert "ck_reactivation_delivery_status" in text
    assert "uq_reactivation_delivery_recipient" in text
