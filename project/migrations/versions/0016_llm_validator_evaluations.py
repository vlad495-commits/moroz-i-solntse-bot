"""Seed immutable LLM Validator evaluation cases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016_llm_validator"
down_revision = "0015_llm_input_security"
branch_labels = None
depends_on = None

VALIDATOR_DATASET_SHA256 = (
    "33291e9eecc45103c5a7b7ae173ad78d7796e454d5f26af4d80b65fa1448e7bc"
)


def _load_validator_cases(path: Path) -> list[dict]:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(data).hexdigest() != VALIDATOR_DATASET_SHA256:
        raise RuntimeError("Validator dataset integrity mismatch for migration 0016")
    return json.loads(data)


VALIDATOR_CASES = _load_validator_cases(
    Path(__file__).parents[2] / "llm" / "eval" / "validator_dataset.json"
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
                "suite": "validator",
                "case_key": case["case_key"],
                "category": case["category"],
                "question": case["input"],
                "expected_answer": "",
                "input_data": {
                    "input": case["input"],
                    "context": case["context"],
                    "route_metadata": case["route_metadata"],
                    "candidate": case["candidate"],
                },
                "expected_data": {
                    "action": case["expected_action"],
                    "source": case["expected_source"],
                    "reason_code": case["expected_reason_code"],
                },
                "critical": case["critical"],
            }
            for case in VALIDATOR_CASES
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM eval_results WHERE run_id IN "
        "(SELECT id FROM eval_runs WHERE suite = 'validator')"
    )
    op.execute("DELETE FROM eval_runs WHERE suite = 'validator'")
    op.execute("DELETE FROM eval_cases WHERE suite = 'validator'")
