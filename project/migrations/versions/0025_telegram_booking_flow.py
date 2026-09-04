"""Allow one active Telegram booking flow per customer."""

from alembic import op
import sqlalchemy as sa


revision = "0025_telegram_booking_flow"
down_revision = "0024_reactivation_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT customer_id
                FROM booking_scenarios
                WHERE phase IN ('collecting', 'awaiting_confirmation', 'executing')
                GROUP BY customer_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION 'duplicate open booking scenarios';
            END IF;
        END $$
        """
    )
    op.create_index(
        "uq_booking_scenarios_open_customer",
        "booking_scenarios",
        ["customer_id"],
        unique=True,
        postgresql_where=sa.text(
            "phase IN ('collecting', 'awaiting_confirmation', 'executing')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_booking_scenarios_open_customer",
        table_name="booking_scenarios",
    )
