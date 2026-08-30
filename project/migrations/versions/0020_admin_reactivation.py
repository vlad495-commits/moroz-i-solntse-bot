"""Add admin reactivation settings, consent and internal campaign queue."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0020_admin_reactivation"
down_revision = "0019_router_v2"
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
        "marketing_consents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("consent_version", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("granted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.UniqueConstraint(
            "channel",
            "user_id",
            name="uq_marketing_consents_channel_user",
        ),
        sa.CheckConstraint(
            "(active AND granted_at IS NOT NULL AND revoked_at IS NULL) "
            "OR (NOT active)",
            name="ck_marketing_consent_state",
        ),
    )
    op.create_index(
        "ix_marketing_consents_active_channel",
        "marketing_consents",
        ["active", "channel"],
    )

    op.create_table(
        "reactivation_settings",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("after_visit_days", sa.Integer(), nullable=False),
        sa.Column("sleeping_days", sa.Integer(), nullable=False),
        sa.Column("discount_percent", sa.Integer(), nullable=False),
        sa.Column("monthly_message_limit", sa.Integer(), nullable=False),
        sa.Column("ignore_limit", sa.Integer(), nullable=False),
        sa.Column("base_offer", sa.Text(), nullable=False, server_default=""),
        sa.Column("llm_instruction", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_reactivation_settings_singleton"),
        sa.CheckConstraint(
            "after_visit_days BETWEEN 0 AND 3650",
            name="ck_reactivation_after_visit_days",
        ),
        sa.CheckConstraint(
            "sleeping_days BETWEEN 1 AND 3650",
            name="ck_reactivation_sleeping_days",
        ),
        sa.CheckConstraint(
            "discount_percent BETWEEN 0 AND 100",
            name="ck_reactivation_discount",
        ),
        sa.CheckConstraint(
            "monthly_message_limit BETWEEN 0 AND 100",
            name="ck_reactivation_monthly_limit",
        ),
        sa.CheckConstraint(
            "ignore_limit BETWEEN 0 AND 100",
            name="ck_reactivation_ignore_limit",
        ),
    )
    op.execute(
        """
        INSERT INTO reactivation_settings
            (id, after_visit_days, sleeping_days, discount_percent,
             monthly_message_limit, ignore_limit, base_offer,
             llm_instruction, updated_at)
        VALUES (1, 1, 90, 0, 1, 2, '', '', now())
        """
    )

    op.create_table(
        "reactivation_campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("segment", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("after_visit_days", sa.Integer(), nullable=False),
        sa.Column("sleeping_days", sa.Integer(), nullable=False),
        sa.Column("discount_percent", sa.Integer(), nullable=False),
        sa.Column("base_offer", sa.Text(), nullable=False),
        sa.Column("llm_instruction", sa.Text(), nullable=False),
        sa.Column("recipient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("queued_at", sa.DateTime(timezone=True)),
        *_timestamps(),
        sa.CheckConstraint(
            "segment IN ('after_visit', 'sleeping', 'regular')",
            name="ck_reactivation_campaign_segment",
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'queued')",
            name="ck_reactivation_campaign_status",
        ),
    )

    op.create_table(
        "reactivation_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reactivation_campaigns.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("skip_reason", sa.Text()),
        sa.Column("error_code", sa.Text()),
        *_timestamps(),
        sa.UniqueConstraint(
            "campaign_id",
            "channel",
            "user_id",
            name="uq_reactivation_delivery_recipient",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'skipped', 'sent', 'error')",
            name="ck_reactivation_delivery_status",
        ),
    )
    op.create_index(
        "ix_reactivation_deliveries_campaign_status",
        "reactivation_deliveries",
        ["campaign_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_reactivation_deliveries_campaign_status",
        table_name="reactivation_deliveries",
    )
    op.drop_table("reactivation_deliveries")
    op.drop_table("reactivation_campaigns")
    op.drop_table("reactivation_settings")
    op.drop_index(
        "ix_marketing_consents_active_channel",
        table_name="marketing_consents",
    )
    op.drop_table("marketing_consents")
