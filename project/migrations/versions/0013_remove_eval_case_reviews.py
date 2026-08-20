"""Remove the retired eval case review module storage."""

from alembic import op
import sqlalchemy as sa


revision = "0013_remove_eval_case_reviews"
down_revision = "0012_projection_suppression"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("eval_case_reviews")


def downgrade() -> None:
    op.create_table(
        "eval_case_reviews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "case_id",
            sa.BigInteger(),
            sa.ForeignKey("eval_cases.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "status",
            sa.String(32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("reviewer", sa.String(64)),
        sa.Column("comment", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("proposed_question", sa.Text()),
        sa.Column("proposed_answer", sa.Text()),
        sa.Column("category", sa.String(64)),
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
    op.create_index(
        "idx_eval_case_reviews_case_id",
        "eval_case_reviews",
        ["case_id"],
        unique=True,
        postgresql_where=sa.text("case_id IS NOT NULL"),
    )
    op.create_index(
        "idx_eval_case_reviews_status",
        "eval_case_reviews",
        ["status", sa.text("updated_at DESC")],
    )
