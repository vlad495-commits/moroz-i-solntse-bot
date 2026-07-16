"""Add versioned processing consents."""

from alembic import op
import sqlalchemy as sa


revision = "0003_processing_consents"
down_revision = "0002_messaging_inbox_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processing_consents",
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("consent_version", sa.Text(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("channel", "user_id", "consent_version"),
    )


def downgrade() -> None:
    op.drop_table("processing_consents")
