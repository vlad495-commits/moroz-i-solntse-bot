"""Add immutable structured Router V3 evaluation cases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0026_router_v3"
down_revision = "0025_telegram_booking_flow"
branch_labels = None
depends_on = None

ROUTER_V3_DATASET_SHA256 = (
    "a0d23048e2ce0d396985bb82ff0832d7b63f0a0f7296227554cb0f6984d736a1"
)


def _load_router_v3_cases(path: Path) -> list[dict]:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ROUTER_V3_DATASET_SHA256:
        raise RuntimeError("Router v3 dataset integrity mismatch for migration 0026")
    return json.loads(data)


ROUTER_V3_CASES = _load_router_v3_cases(
    Path(__file__).parents[2] / "llm" / "eval" / "router_dataset_v3.json"
)


def upgrade() -> None:
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
                "suite": "router_v3",
                "case_key": case["case_key"],
                "category": case["category"],
                "question": case["input"],
                "expected_answer": "",
                "input_data": {
                    "input": case["input"],
                    "context": case["context"],
                },
                "expected_data": case["expected"],
                "critical": case["critical"],
            }
            for case in ROUTER_V3_CASES
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM eval_results WHERE run_id IN "
        "(SELECT id FROM eval_runs WHERE suite = 'router_v3') OR case_id IN "
        "(SELECT id FROM eval_cases WHERE suite = 'router_v3')"
    )
    op.execute("DELETE FROM eval_runs WHERE suite = 'router_v3'")
    op.execute("DELETE FROM eval_cases WHERE suite = 'router_v3'")
