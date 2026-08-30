"""Add owner statistics settings and period indexes."""

from alembic import op
import sqlalchemy as sa


revision = "0022_admin_statistics"
down_revision = "0021_admin_reactivation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "admin_statistics_settings",
        sa.Column(
            "id",
            sa.Boolean(),
            primary_key=True,
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("minutes_per_dialogue", sa.Numeric(10, 2)),
        sa.Column("hourly_rate_rub", sa.Numeric(12, 2)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "id",
            name="ck_admin_statistics_settings_singleton",
        ),
        sa.CheckConstraint(
            "minutes_per_dialogue > 0",
            name="ck_admin_statistics_minutes_positive",
        ),
        sa.CheckConstraint(
            "hourly_rate_rub > 0",
            name="ck_admin_statistics_rate_positive",
        ),
    )
    op.create_index("ix_messages_created_at", "messages", ["created_at"])
    op.create_index("ix_token_usage_created_at", "token_usage", ["created_at"])
    op.create_index(
        "ix_outbound_messages_created_at",
        "outbound_messages",
        ["created_at"],
    )
    op.create_index("ix_escalations_created_at", "escalations", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_escalations_created_at", table_name="escalations")
    op.drop_index(
        "ix_outbound_messages_created_at",
        table_name="outbound_messages",
    )
    op.drop_index("ix_token_usage_created_at", table_name="token_usage")
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_table("admin_statistics_settings")
