"""Add common Router evaluation columns and immutable initial cases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0014_llm_router_evaluations"
down_revision = "0013_remove_eval_case_reviews"
branch_labels = None
depends_on = None

ROUTER_DATASET_SHA256 = (
    "87c8eb45783c44d7760d0ac2c69b957325fa3a22490d1551c0991fe620004f84"
)


def _load_router_cases(path: Path) -> list[dict]:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ROUTER_DATASET_SHA256:
        raise RuntimeError("Router dataset integrity mismatch for migration 0014")
    return json.loads(data)


ROUTER_CASES = _load_router_cases(
    Path(__file__).parents[2] / "llm" / "eval" / "router_dataset.json"
)


def upgrade() -> None:
    op.add_column("eval_cases",
        sa.Column(
            "suite", sa.String(32), nullable=False, server_default="answer"
        ),
    )
    op.add_column("eval_cases", sa.Column("case_key", sa.String(96)))
    op.add_column(
        "eval_cases",
        sa.Column(
            "input_data",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "eval_cases",
        sa.Column(
            "expected_data",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "eval_cases",
        sa.Column(
            "critical",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "uq_eval_cases_suite_case_key",
        "eval_cases",
        ["suite", "case_key"],
        unique=True,
        postgresql_where=sa.text("case_key IS NOT NULL"),
    )
    op.add_column("eval_runs",
        sa.Column(
            "suite", sa.String(32), nullable=False, server_default="answer"
        ),
    )
    op.add_column("eval_results",
        sa.Column(
            "actual_data",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("token_usage",
        sa.Column(
            "purpose",
            sa.String(32),
            nullable=False,
            server_default="answer",
        ),
    )
    cases = sa.table(
        "eval_cases",
        sa.column("suite", sa.String),
        sa.column("case_key", sa.String),
        sa.column("category", sa.String),
        sa.column("question", sa.Text),
        sa.column("expected_answer", sa.Text),
        sa.column("input_data", postgresql.JSONB),
        sa.column("expected_data", postgresql.JSONB),
        sa.column("critical", sa.Boolean),
    )
    op.bulk_insert(
        cases,
        [
            {
                "suite": "router",
                "case_key": case["case_key"],
                "category": case["category"],
                "question": case["input"],
                "expected_answer": "",
                "input_data": {
                    "input": case["input"],
                    "context": case["context"],
                },
                "expected_data": {
                    "intents": case["expected_intents"],
                    "requires_clarification": case["expected_clarification"],
                    "source": case["expected_source"],
                },
                "critical": case["critical"],
            }
            for case in ROUTER_CASES
        ],
    )


def downgrade() -> None:
    # The legacy schema cannot represent Router suite rows. Remove only rows
    # owned by this suite and preserve every answer case, run, and result.
    op.execute(
        "DELETE FROM eval_results WHERE run_id IN "
        "(SELECT id FROM eval_runs WHERE suite = 'router') OR case_id IN "
        "(SELECT id FROM eval_cases WHERE suite = 'router')"
    )
    op.execute("DELETE FROM eval_runs WHERE suite = 'router'")
    op.execute("DELETE FROM eval_cases WHERE suite = 'router'")
    op.drop_column("token_usage", "purpose")
    op.drop_column("eval_results", "actual_data")
    op.drop_column("eval_runs", "suite")
    op.drop_index("uq_eval_cases_suite_case_key", table_name="eval_cases")
    op.drop_column("eval_cases", "critical")
    op.drop_column("eval_cases", "expected_data")
    op.drop_column("eval_cases", "input_data")
    op.drop_column("eval_cases", "case_key")
    op.drop_column("eval_cases", "suite")
