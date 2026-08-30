EXPECTED_TABLES = {
    "customer_activity_projection",
    "marketing_consent_events",
    "reactivation_program_versions",
    "reactivation_journeys",
    "reactivation_journey_steps",
}


def test_reactivation_v2_migration_contract(migration_source: str) -> None:
    assert 'revision = "0023_reactivation_v2"' in migration_source
    assert 'down_revision = "0022_admin_statistics"' in migration_source
    for table in EXPECTED_TABLES:
        assert f'"{table}"' in migration_source
    assert "legacy_unproven" in migration_source
    assert "delivery_unknown" in migration_source
    assert "reactivation:{journey_id}:{step_kind}" not in migration_source


def test_reactivation_v2_migration_names_all_constraints_and_indexes(
    migration_source: str,
) -> None:
    for name in (
        "ck_customer_activity_projection_identity_status",
        "ck_customer_activity_projection_sync_status",
        "uq_customer_activity_projection_verified_yclients_client",
        "ck_marketing_consent_events_action",
        "uq_marketing_consent_events_source_event",
        "ck_reactivation_program_versions_status",
        "uq_reactivation_program_versions_number",
        "uq_reactivation_program_versions_active",
        "ck_reactivation_program_versions_inactivity_days",
        "ck_reactivation_program_versions_reminder_after_days",
        "ck_reactivation_program_versions_cooldown_days",
        "ck_reactivation_program_versions_main_text",
        "ck_reactivation_program_versions_reminder_text",
        "ck_reactivation_journeys_status",
        "ck_reactivation_journeys_close_reason",
        "uq_reactivation_journeys_open_customer",
        "ck_reactivation_journey_steps_kind",
        "ck_reactivation_journey_steps_status",
        "uq_reactivation_journey_steps_journey_kind",
        "uq_reactivation_journey_steps_idempotency_key",
        "uq_reactivation_journey_steps_outbound_id",
        "ck_reactivation_settings_mode",
        "ck_reactivation_settings_legal_status",
    ):
        assert name in migration_source


def test_reactivation_v2_downgrade_removes_references_before_tables(
    migration_source: str,
) -> None:
    assert migration_source.index('op.drop_column("marketing_consents", "proof_event_id")') < (
        migration_source.index('op.drop_table("marketing_consent_events")')
    )
    assert migration_source.index('op.drop_column("reactivation_settings", "active_version_id")') < (
        migration_source.index('op.drop_table("reactivation_program_versions")')
    )
    assert migration_source.index('op.drop_table("reactivation_journey_steps")') < (
        migration_source.index('op.drop_table("reactivation_journeys")')
    )
    assert migration_source.index('op.drop_table("reactivation_journeys")') < (
        migration_source.index('op.drop_table("reactivation_program_versions")')
    )
