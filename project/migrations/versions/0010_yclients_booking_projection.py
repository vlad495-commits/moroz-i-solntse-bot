"""Add bounded YCLIENTS booking projection."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_yclients_projection"
down_revision = "0009_production_admin"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yclients_booking_projection",
        sa.Column("external_id", sa.Text(), primary_key=True),
        sa.Column("booking_key", postgresql.UUID(as_uuid=True)),
        sa.Column("bot_marker_state", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("client_name", sa.Text()),
        sa.Column("staff_name", sa.Text()),
        sa.Column("service_names", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "bot_marker_state IN ('absent','valid','invalid')",
            name="ck_yclients_projection_marker",
        ),
        sa.CheckConstraint(
            "status IN ('confirmed','cancelled','completed','no_show','unknown')",
            name="ck_yclients_projection_status",
        ),
    )
    op.create_index(
        "ix_yclients_projection_starts_external",
        "yclients_booking_projection",
        ["starts_at", "external_id"],
    )
    op.create_index(
        "ix_yclients_projection_booking_key",
        "yclients_booking_projection",
        ["booking_key"],
        postgresql_where=sa.text("booking_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_yclients_projection_booking_key",
        table_name="yclients_booking_projection",
    )
    op.drop_index(
        "ix_yclients_projection_starts_external",
        table_name="yclients_booking_projection",
    )
    op.drop_table("yclients_booking_projection")
