from pathlib import Path


MIGRATION = Path("/workspace/migrations/versions/0020_message_llm_analytics.py")


def test_migration_adds_nullable_tracking_and_exact_usage_link():
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "0020_message_llm_analytics"' in text
    assert 'down_revision = "0019_router_v2"' in text
    assert text.count("op.add_column(") == 2
    assert '"llm_usage_tracked"' in text
    assert '"source_message_id"' in text
    assert 'ondelete="CASCADE"' in text
    assert '"idx_token_usage_source_message_id"' in text
    assert "server_default" not in text
    assert "UPDATE messages" not in text


def test_migration_downgrade_removes_owned_objects_in_dependency_order():
    text = MIGRATION.read_text(encoding="utf-8")
    downgrade = text.split("def downgrade", 1)[1]

    index = downgrade.index('op.drop_index("idx_token_usage_source_message_id"')
    constraint = downgrade.index("op.drop_constraint")
    usage_column = downgrade.index(
        'op.drop_column("token_usage", "source_message_id")'
    )
    message_column = downgrade.index(
        'op.drop_column("messages", "llm_usage_tracked")'
    )
    assert index < constraint < usage_column < message_column
