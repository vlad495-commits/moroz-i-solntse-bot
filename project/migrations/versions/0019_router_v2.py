"""Add immutable single-route Router evaluation cases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0019_router_v2"
down_revision = "0018_simple_security"
branch_labels = None
depends_on = None

ROUTER_V2_DATASET_SHA256 = (
    "55b3d8d58a7112c7a53df0c52968690cc4403e8e77c17d5fd91189f6c0e0ebf0"
)


def _load_router_v2_cases(path: Path) -> list[dict]:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ROUTER_V2_DATASET_SHA256:
        raise RuntimeError("Router v2 dataset integrity mismatch for migration 0019")
    return json.loads(data)


ROUTER_V2_CASES = _load_router_v2_cases(
    Path(__file__).parents[2] / "llm" / "eval" / "router_dataset_v2.json"
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
                "suite": "router_v2",
                "case_key": case["case_key"],
                "category": case["category"],
                "question": case["input"],
                "expected_answer": "",
                "input_data": {
                    "input": case["input"],
                    "context": case["context"],
                },
                "expected_data": {"route": case["expected_route"]},
                "critical": case["critical"],
            }
            for case in ROUTER_V2_CASES
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM eval_results WHERE run_id IN "
        "(SELECT id FROM eval_runs WHERE suite = 'router_v2') OR case_id IN "
        "(SELECT id FROM eval_cases WHERE suite = 'router_v2')"
    )
    op.execute("DELETE FROM eval_runs WHERE suite = 'router_v2'")
    op.execute("DELETE FROM eval_cases WHERE suite = 'router_v2'")
