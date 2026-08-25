"""Seed immutable LLM Input Security evaluation cases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0015_llm_input_security"
down_revision = "0014_llm_router_evaluations"
branch_labels = None
depends_on = None

SECURITY_DATASET_SHA256 = (
    "87bb9abf355ffb2a9bcf300f00687dfd5eb4acf9b8e558730749e82169177a3c"
)


def _load_security_cases(path: Path) -> list[dict]:
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != SECURITY_DATASET_SHA256:
        raise RuntimeError("Security dataset integrity mismatch for migration 0015")
    return json.loads(data)


SECURITY_CASES = _load_security_cases(
    Path(__file__).parents[2] / "llm" / "eval" / "security_dataset.json"
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
                "suite": "security",
                "case_key": case["case_key"],
                "category": case["category"],
                "question": case["input"],
                "expected_answer": "",
                "input_data": {
                    "input": case["input"],
                    "context": case["context"],
                },
                "expected_data": {
                    "action": case["expected_action"],
                    "source": case["expected_source"],
                },
                "critical": case["critical"],
            }
            for case in SECURITY_CASES
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM eval_results WHERE run_id IN "
        "(SELECT id FROM eval_runs WHERE suite = 'security')"
    )
    op.execute("DELETE FROM eval_runs WHERE suite = 'security'")
    op.execute("DELETE FROM eval_cases WHERE suite = 'security'")
