"""Link token usage to an observed user message."""

from alembic import op
import sqlalchemy as sa


revision = "0020_message_llm_analytics"
down_revision = "0019_router_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("llm_usage_tracked", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "token_usage",
        sa.Column("source_message_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_token_usage_source_message_id_messages",
        "token_usage",
        "messages",
        ["source_message_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "idx_token_usage_source_message_id",
        "token_usage",
        ["source_message_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_token_usage_source_message_id", table_name="token_usage")
    op.drop_constraint(
        "fk_token_usage_source_message_id_messages",
        "token_usage",
        type_="foreignkey",
    )
    op.drop_column("token_usage", "source_message_id")
    op.drop_column("messages", "llm_usage_tracked")
