"""Add durable inbox order and outbound delivery claim timestamp."""

from alembic import op
import sqlalchemy as sa


revision = "0004_pipeline_order_claim"
down_revision = "0003_processing_consents"
branch_labels = None
depends_on = None


SEQUENCE = "message_inbox_ingress_sequence_seq"


def upgrade() -> None:
    op.add_column(
        "message_inbox",
        sa.Column("ingress_sequence", sa.BigInteger(), nullable=True),
    )
    op.execute(f"CREATE SEQUENCE {SEQUENCE}")
    op.execute(
        f"ALTER SEQUENCE {SEQUENCE} OWNED BY message_inbox.ingress_sequence"
    )
    op.execute(
        f"""
        WITH ordered AS (
            SELECT id, row_number() OVER (ORDER BY created_at, id) AS sequence
            FROM message_inbox
        )
        UPDATE message_inbox AS inbox
        SET ingress_sequence = ordered.sequence
        FROM ordered
        WHERE inbox.id = ordered.id
        """
    )
    op.execute(
        f"""
        SELECT setval(
            '{SEQUENCE}',
            COALESCE(MAX(ingress_sequence), 1),
            MAX(ingress_sequence) IS NOT NULL
        )
        FROM message_inbox
        """
    )
    op.alter_column(
        "message_inbox",
        "ingress_sequence",
        nullable=False,
        server_default=sa.text(f"nextval('{SEQUENCE}'::regclass)"),
    )
    op.create_unique_constraint(
        "uq_message_inbox_ingress_sequence",
        "message_inbox",
        ["ingress_sequence"],
    )
    op.add_column(
        "outbound_messages",
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("outbound_messages", "claimed_at")
    op.drop_constraint(
        "uq_message_inbox_ingress_sequence",
        "message_inbox",
        type_="unique",
    )
    op.drop_column("message_inbox", "ingress_sequence")
