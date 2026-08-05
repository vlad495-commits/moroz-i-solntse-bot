"""Add durable Telegram booking workflow state and actions."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_telegram_booking_flow"
down_revision = "0009_production_admin"
branch_labels = None
depends_on = None


_ACTIVE_PHASES = "('collecting', 'awaiting_confirmation', 'executing')"


def upgrade() -> None:
    op.add_column(
        "booking_scenarios",
        sa.Column("channel", sa.Text(), nullable=True),
    )
    op.add_column(
        "booking_scenarios",
        sa.Column("chat_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "booking_scenarios",
        sa.Column(
            "revision",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "booking_scenarios",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE booking_scenarios
        SET channel = 'telegram', chat_id = customer_id
        WHERE channel IS NULL OR chat_id IS NULL
        """
    )
    op.create_check_constraint(
        "ck_booking_scenarios_revision",
        "booking_scenarios",
        "revision >= 0",
    )
    op.create_index(
        "uq_booking_scenarios_active_telegram_identity",
        "booking_scenarios",
        ["channel", "chat_id", "customer_id"],
        unique=True,
        postgresql_where=sa.text(
            f"channel = 'telegram' AND phase IN {_ACTIVE_PHASES}"
        ),
    )

    op.create_table(
        "booking_actions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "scenario_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("booking_scenarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action_kind", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.CheckConstraint("revision >= 0", name="ck_booking_actions_revision"),
        sa.CheckConstraint(
            "(consumed_at IS NULL AND result IS NULL) OR "
            "(consumed_at IS NOT NULL AND result IS NOT NULL)",
            name="ck_booking_actions_consumption",
        ),
    )
    op.create_index(
        "ix_booking_actions_scenario_revision",
        "booking_actions",
        ["scenario_id", "revision"],
    )

    op.add_column(
        "escalations",
        sa.Column("resolved_by", sa.Text(), nullable=True),
    )
    op.add_column(
        "escalations",
        sa.Column("resolution_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("escalations", "resolution_reason")
    op.drop_column("escalations", "resolved_by")
    op.drop_index("ix_booking_actions_scenario_revision", table_name="booking_actions")
    op.drop_table("booking_actions")
    op.drop_index(
        "uq_booking_scenarios_active_telegram_identity",
        table_name="booking_scenarios",
    )
    op.drop_constraint(
        "ck_booking_scenarios_revision",
        "booking_scenarios",
        type_="check",
    )
    op.drop_column("booking_scenarios", "expires_at")
    op.drop_column("booking_scenarios", "revision")
    op.drop_column("booking_scenarios", "chat_id")
    op.drop_column("booking_scenarios", "channel")
