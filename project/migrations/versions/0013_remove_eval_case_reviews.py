"""Retire the eval review module while retaining rollback-compatible storage."""

revision = "0013_remove_eval_case_reviews"
down_revision = "0012_projection_suppression"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Contract migration is deferred until the previous admin image expires.
    pass


def downgrade() -> None:
    pass
