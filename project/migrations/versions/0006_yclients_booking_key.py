"""Add a deterministic booking ownership key."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0006_yclients_booking_key"
down_revision = "0005_booking_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bookings", sa.Column("booking_key", postgresql.UUID(as_uuid=True))
    )
    op.execute("UPDATE bookings SET booking_key = id WHERE booking_key IS NULL")
    op.alter_column("bookings", "booking_key", nullable=False)
    op.create_unique_constraint("uq_bookings_booking_key", "bookings", ["booking_key"])


def downgrade() -> None:
    op.drop_constraint("uq_bookings_booking_key", "bookings", type_="unique")
    op.drop_column("bookings", "booking_key")
