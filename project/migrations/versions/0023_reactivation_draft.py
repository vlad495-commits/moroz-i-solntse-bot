"""Allow materialized reactivation draft recipients."""

from alembic import op


revision = "0023_reactivation_draft"
down_revision = "0022_admin_statistics"
branch_labels = None
depends_on = None


def _replace_status(values: str) -> None:
    op.drop_constraint(
        "ck_reactivation_delivery_status",
        "reactivation_deliveries",
        type_="check",
    )
    op.create_check_constraint(
        "ck_reactivation_delivery_status",
        "reactivation_deliveries",
        f"status IN ({values})",
    )


def upgrade() -> None:
    _replace_status("'draft', 'queued', 'skipped', 'sent', 'error'")


def downgrade() -> None:
    op.execute(
        "UPDATE reactivation_deliveries SET status = 'queued' "
        "WHERE status = 'draft'"
    )
    _replace_status("'queued', 'skipped', 'sent', 'error'")
