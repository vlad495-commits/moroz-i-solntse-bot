"""Add scheduler jobs and notification state."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0007_scheduler_notifications"
down_revision = "0006_yclients_booking_key"
branch_labels = None
depends_on = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
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
    )


def upgrade() -> None:
    op.create_table(
        "scheduler_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("booking_key", postgresql.UUID(as_uuid=True)),
        sa.Column("booking_starts_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'finished', 'skipped', 'failed')",
            name="ck_scheduler_jobs_status",
        ),
    )
    op.create_index(
        "ix_scheduler_jobs_status_run_at",
        "scheduler_jobs",
        ["status", "run_at"],
    )
    op.create_index(
        "ix_scheduler_jobs_booking_key_status",
        "scheduler_jobs",
        ["booking_key", "status"],
    )
    op.create_table(
        "notification_feedback_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", sa.Text(), nullable=False, unique=True),
        sa.Column("booking_key", postgresql.UUID(as_uuid=True)),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_table(
        "escalations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("customer_id", sa.Text(), nullable=False),
        sa.Column("booking_key", postgresql.UUID(as_uuid=True)),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_escalations_status",
        ),
    )
    op.create_table(
        "human_mode",
        sa.Column("customer_id", sa.Text(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("escalation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("human_mode")
    op.drop_table("escalations")
    op.drop_table("notification_feedback_requests")
    op.drop_index("ix_scheduler_jobs_booking_key_status", table_name="scheduler_jobs")
    op.drop_index("ix_scheduler_jobs_status_run_at", table_name="scheduler_jobs")
    op.drop_table("scheduler_jobs")

