"""Add bounded YCLIENTS service catalog projection."""

from alembic import op
import sqlalchemy as sa


revision = "0011_yclients_service_catalog"
down_revision = "0010_yclients_projection"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "yclients_service_catalog",
        sa.Column("service_id", sa.Text(), primary_key=True),
        sa.Column("staff_id", sa.Text(), primary_key=True),
        sa.Column("service_name", sa.Text(), nullable=False),
        sa.Column("category_name", sa.Text()),
        sa.Column("staff_name", sa.Text(), nullable=False),
        sa.Column("price_min", sa.Numeric(10, 2), nullable=False),
        sa.Column("price_max", sa.Numeric(10, 2), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "price_min >= 0 AND price_max >= price_min "
            "AND price_max <= 99999999.99",
            name="ck_yclients_catalog_price",
        ),
        sa.CheckConstraint(
            "duration_minutes BETWEEN 1 AND 1440",
            name="ck_yclients_catalog_duration",
        ),
    )


def downgrade() -> None:
    op.drop_table("yclients_service_catalog")
