"""Suppress rematerialization of locally deleted YCLIENTS projections."""

from alembic import op
import sqlalchemy as sa


revision = "0012_projection_suppression"
down_revision = "0011_yclients_service_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yclients_projection_suppressions",
        sa.Column("external_id", sa.Text(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("yclients_projection_suppressions")
