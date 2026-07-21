"""Add durable booking scenarios, snapshots, and events."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_booking_state"
down_revision = "0004_pipeline_order_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "booking_scenarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("error_code", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "phase IN ('collecting', 'awaiting_confirmation', 'executing', "
            "'confirmed', 'failed', 'escalated')",
            name="ck_booking_scenarios_phase",
        ),
    )
    op.create_table(
        "bookings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "last_scenario_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("booking_scenarios.id"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text(), nullable=False, unique=True),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("slot_id", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'cancelled')",
            name="ck_bookings_status",
        ),
    )
    op.create_table(
        "booking_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scenario_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("booking_scenarios.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_booking_events_scenario_created",
        "booking_events",
        ["scenario_id", "created_at"],
    )
    op.create_index(
        "ix_bookings_customer_starts",
        "bookings",
        ["customer_id", "starts_at"],
    )


def downgrade() -> None:
    op.drop_table("booking_events")
    op.drop_table("bookings")
    op.drop_table("booking_scenarios")
