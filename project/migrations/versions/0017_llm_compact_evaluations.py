"""Seed immutable LLM Compact evaluation cases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0017_llm_compact"
down_revision = "0016_llm_validator"
branch_labels = None
depends_on = None

COMPACT_DATASET_SHA256 = (
    "ad214fddac499c586d7dd08c67c19dce31fe9f4b0da54e5f8a86a1597ab7b442"
)


def _load_compact_cases(path: Path) -> list[dict]:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(data).hexdigest() != COMPACT_DATASET_SHA256:
        raise RuntimeError("Compact dataset integrity mismatch for migration 0017")
    return json.loads(data)


COMPACT_CASES = _load_compact_cases(
    Path(__file__).parents[2] / "llm" / "eval" / "compact_dataset.json"
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
                "suite": "compact",
                "case_key": case["case_key"],
                "category": case["category"],
                "question": (
                    f"Compact context: {case['category']} "
                    f"({len(case['context'])} messages)"
                ),
                "expected_answer": "",
                "input_data": {
                    "context": case["context"],
                    "expected_mode": case["expected_mode"],
                },
                "expected_data": {
                    "required_facts": case["required_facts"],
                    "forbidden_facts": case["forbidden_facts"],
                },
                "critical": case["critical"],
            }
            for case in COMPACT_CASES
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM eval_results WHERE run_id IN "
        "(SELECT id FROM eval_runs WHERE suite = 'compact')"
    )
    op.execute("DELETE FROM eval_runs WHERE suite = 'compact'")
    op.execute("DELETE FROM eval_cases WHERE suite = 'compact'")
