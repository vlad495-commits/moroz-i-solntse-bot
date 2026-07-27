"""Add durable YCLIENTS visit lifecycle state."""

from alembic import op
import sqlalchemy as sa


revision = "0008_yclients_lifecycle"
down_revision = "0007_scheduler_notifications"
branch_labels = None
depends_on = None


_OLD = "status IN ('confirmed', 'cancelled')"
_NEW = (
    "status IN ('confirmed', 'cancelled', 'completed', 'no_show', 'unknown')"
)


def upgrade() -> None:
    op.add_column(
        "bookings",
        sa.Column("scheduled_end_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", _NEW)


def downgrade() -> None:
    op.execute(
        "UPDATE bookings SET status = 'confirmed' "
        "WHERE status IN ('completed', 'no_show', 'unknown')"
    )
    op.drop_constraint("ck_bookings_status", "bookings", type_="check")
    op.create_check_constraint("ck_bookings_status", "bookings", _OLD)
    op.drop_column("bookings", "scheduled_end_at")
