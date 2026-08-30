"""Add additive Reactivation V2 schema."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0023_reactivation_v2"
down_revision = "0022_admin_statistics"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def upgrade() -> None:
    op.create_table(
        "customer_activity_projection",
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("yclients_client_id", sa.Text()),
        sa.Column("identity_status", sa.Text(), nullable=False),
        sa.Column("identity_source", sa.Text()),
        sa.Column("identity_verified_at", sa.DateTime(timezone=True)),
        sa.Column("last_completed_visit_at", sa.DateTime(timezone=True)),
        sa.Column("last_meaningful_inbound_at", sa.DateTime(timezone=True)),
        sa.Column("next_active_booking_at", sa.DateTime(timezone=True)),
        sa.Column("history_synced_at", sa.DateTime(timezone=True)),
        sa.Column("recent_bookings_synced_at", sa.DateTime(timezone=True)),
        sa.Column("source_version", sa.Text()),
        sa.Column("sync_status", sa.Text(), nullable=False),
        sa.Column("sync_error_code", sa.Text()),
        *_timestamps(),
        sa.PrimaryKeyConstraint("channel", "user_id"),
        sa.CheckConstraint(
            "identity_status IN ('unverified', 'verified', 'conflict')",
            name="ck_customer_activity_projection_identity_status",
        ),
        sa.CheckConstraint(
            "sync_status IN ('never', 'current', 'partial', 'error')",
            name="ck_customer_activity_projection_sync_status",
        ),
    )
    op.create_index(
        "uq_customer_activity_projection_verified_yclients_client",
        "customer_activity_projection",
        ["yclients_client_id"],
        unique=True,
        postgresql_where=sa.text(
            "identity_status = 'verified' AND yclients_client_id IS NOT NULL"
        ),
    )

    op.create_table(
        "marketing_consent_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("consent_version", sa.Text()),
        sa.Column("proof_text_hash", sa.Text()),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_event_id", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "channel",
            "user_id",
            "action",
            "source",
            "source_event_id",
            name="uq_marketing_consent_events_source_event",
        ),
        sa.CheckConstraint(
            "action IN ('granted', 'revoked', 'suppressed', 'unsuppressed')",
            name="ck_marketing_consent_events_action",
        ),
    )

    op.create_table(
        "reactivation_program_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("inactivity_days", sa.Integer(), nullable=False),
        sa.Column("reminder_enabled", sa.Boolean(), nullable=False),
        sa.Column("reminder_after_days", sa.Integer()),
        sa.Column("cooldown_days", sa.Integer(), nullable=False),
        sa.Column("main_text", sa.Text(), nullable=False),
        sa.Column("reminder_text", sa.Text(), nullable=False),
        sa.Column("template_checksum", sa.Text(), nullable=False),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("activated_by", sa.BigInteger()),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("preview_created_at", sa.DateTime(timezone=True)),
        sa.Column("preview_checksum", sa.Text()),
        sa.Column("preview_counts", postgresql.JSONB()),
        sa.Column("preview_population_watermark", sa.DateTime(timezone=True)),
        sa.Column("preview_history_watermark", sa.DateTime(timezone=True)),
        sa.Column("preview_recent_watermark", sa.DateTime(timezone=True)),
        sa.Column("test_outbound_id", postgresql.UUID(as_uuid=True)),
        sa.Column("test_sent_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "version_number", name="uq_reactivation_program_versions_number"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["admin_users.id"],
            name="fk_reactivation_program_versions_created_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["activated_by"],
            ["admin_users.id"],
            name="fk_reactivation_program_versions_activated_by",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["test_outbound_id"],
            ["outbound_messages.id"],
            name="fk_reactivation_program_versions_test_outbound",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ck_reactivation_program_versions_status",
        ),
        sa.CheckConstraint(
            "inactivity_days IN (60, 90, 120)",
            name="ck_reactivation_program_versions_inactivity_days",
        ),
        sa.CheckConstraint(
            "reminder_after_days IS NULL OR reminder_after_days IN (3, 5, 7)",
            name="ck_reactivation_program_versions_reminder_after_days",
        ),
        sa.CheckConstraint(
            "cooldown_days >= inactivity_days",
            name="ck_reactivation_program_versions_cooldown_days",
        ),
        sa.CheckConstraint(
            "char_length(main_text) BETWEEN 1 AND 4096",
            name="ck_reactivation_program_versions_main_text",
        ),
        sa.CheckConstraint(
            "char_length(reminder_text) BETWEEN 1 AND 4096",
            name="ck_reactivation_program_versions_reminder_text",
        ),
    )
    op.create_index(
        "uq_reactivation_program_versions_active",
        "reactivation_program_versions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "reactivation_journeys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("program_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("close_reason", sa.Text()),
        sa.Column("activity_anchor_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_sent_at", sa.DateTime(timezone=True)),
        sa.Column("replied_at", sa.DateTime(timezone=True)),
        sa.Column("booked_at", sa.DateTime(timezone=True)),
        sa.Column("completed_visit_at", sa.DateTime(timezone=True)),
        sa.Column("escalated_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["program_version_id"],
            ["reactivation_program_versions.id"],
            name="fk_reactivation_journeys_program_version",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'active', 'closed')",
            name="ck_reactivation_journeys_status",
        ),
        sa.CheckConstraint(
            "close_reason IS NULL OR close_reason IN "
            "('responded', 'booked', 'suppressed', 'exhausted', 'failed', "
            "'cancelled', 'delivery_unknown', 'escalated')",
            name="ck_reactivation_journeys_close_reason",
        ),
    )
    op.create_index(
        "uq_reactivation_journeys_open_customer",
        "reactivation_journeys",
        ["channel", "user_id"],
        unique=True,
        postgresql_where=sa.text("status != 'closed'"),
    )

    op.create_table(
        "reactivation_journey_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("journey_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("outbound_id", postgresql.UUID(as_uuid=True)),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("terminal_reason", sa.Text()),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["journey_id"],
            ["reactivation_journeys.id"],
            name="fk_reactivation_journey_steps_journey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["outbound_id"],
            ["outbound_messages.id"],
            name="fk_reactivation_journey_steps_outbound",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "journey_id",
            "step_kind",
            name="uq_reactivation_journey_steps_journey_kind",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_reactivation_journey_steps_idempotency_key",
        ),
        sa.CheckConstraint(
            "step_kind IN ('main', 'reminder')",
            name="ck_reactivation_journey_steps_kind",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'reserved', 'sent', 'delivery_unknown', "
            "'skipped', 'cancelled', 'failed')",
            name="ck_reactivation_journey_steps_status",
        ),
    )
    op.create_index(
        "uq_reactivation_journey_steps_outbound_id",
        "reactivation_journey_steps",
        ["outbound_id"],
        unique=True,
        postgresql_where=sa.text("outbound_id IS NOT NULL"),
    )

    op.add_column(
        "marketing_consents",
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default="legacy_unproven",
        ),
    )
    op.add_column("marketing_consents", sa.Column("proof_event_id", postgresql.UUID(as_uuid=True)))
    op.add_column("marketing_consents", sa.Column("proof_text_hash", sa.Text()))
    op.add_column("marketing_consents", sa.Column("suppressed_at", sa.DateTime(timezone=True)))
    op.add_column("marketing_consents", sa.Column("suppression_reason", sa.Text()))
    op.add_column("marketing_consents", sa.Column("suppression_source", sa.Text()))
    op.create_foreign_key(
        "fk_marketing_consents_proof_event",
        "marketing_consents",
        "marketing_consent_events",
        ["proof_event_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        "UPDATE marketing_consents SET source = 'legacy_unproven', "
        "proof_event_id = NULL, active = false"
    )
    op.alter_column("marketing_consents", "source", server_default=None)

    op.add_column(
        "reactivation_settings",
        sa.Column("mode", sa.Text(), nullable=False, server_default="dry_run"),
    )
    op.add_column("reactivation_settings", sa.Column("active_version_id", postgresql.UUID(as_uuid=True)))
    op.add_column(
        "reactivation_settings",
        sa.Column("legal_status", sa.Text(), nullable=False, server_default="pending"),
    )
    op.add_column("reactivation_settings", sa.Column("legal_reference", sa.Text()))
    op.add_column("reactivation_settings", sa.Column("legal_approved_at", sa.DateTime(timezone=True)))
    op.add_column("reactivation_settings", sa.Column("legal_approved_by", sa.BigInteger()))
    op.add_column(
        "reactivation_settings",
        sa.Column("program_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("reactivation_settings", sa.Column("stopped_at", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "fk_reactivation_settings_active_version",
        "reactivation_settings",
        "reactivation_program_versions",
        ["active_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_reactivation_settings_legal_approved_by",
        "reactivation_settings",
        "admin_users",
        ["legal_approved_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_reactivation_settings_mode",
        "reactivation_settings",
        "mode IN ('dry_run', 'paused', 'active')",
    )
    op.create_check_constraint(
        "ck_reactivation_settings_legal_status",
        "reactivation_settings",
        "legal_status IN ('pending', 'approved', 'rejected')",
    )

    op.add_column("yclients_booking_projection", sa.Column("client_id", sa.Text()))
    op.add_column(
        "yclients_booking_projection",
        sa.Column("record_created_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("yclients_booking_projection", "record_created_at")
    op.drop_column("yclients_booking_projection", "client_id")

    op.drop_constraint(
        "ck_reactivation_settings_legal_status",
        "reactivation_settings",
        type_="check",
    )
    op.drop_constraint(
        "ck_reactivation_settings_mode",
        "reactivation_settings",
        type_="check",
    )
    op.drop_constraint(
        "fk_reactivation_settings_legal_approved_by",
        "reactivation_settings",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_reactivation_settings_active_version",
        "reactivation_settings",
        type_="foreignkey",
    )
    op.drop_column("reactivation_settings", "stopped_at")
    op.drop_column("reactivation_settings", "program_revision")
    op.drop_column("reactivation_settings", "legal_approved_by")
    op.drop_column("reactivation_settings", "legal_approved_at")
    op.drop_column("reactivation_settings", "legal_reference")
    op.drop_column("reactivation_settings", "legal_status")
    op.drop_column("reactivation_settings", "active_version_id")
    op.drop_column("reactivation_settings", "mode")

    op.drop_constraint(
        "fk_marketing_consents_proof_event",
        "marketing_consents",
        type_="foreignkey",
    )
    op.drop_column("marketing_consents", "suppression_source")
    op.drop_column("marketing_consents", "suppression_reason")
    op.drop_column("marketing_consents", "suppressed_at")
    op.drop_column("marketing_consents", "proof_text_hash")
    op.drop_column("marketing_consents", "proof_event_id")
    op.drop_column("marketing_consents", "source")

    op.drop_index(
        "uq_reactivation_journey_steps_outbound_id",
        table_name="reactivation_journey_steps",
    )
    op.drop_table("reactivation_journey_steps")
    op.drop_index(
        "uq_reactivation_journeys_open_customer",
        table_name="reactivation_journeys",
    )
    op.drop_table("reactivation_journeys")
    op.drop_index(
        "uq_reactivation_program_versions_active",
        table_name="reactivation_program_versions",
    )
    op.drop_table("reactivation_program_versions")
    op.drop_table("marketing_consent_events")
    op.drop_index(
        "uq_customer_activity_projection_verified_yclients_client",
        table_name="customer_activity_projection",
    )
    op.drop_table("customer_activity_projection")
