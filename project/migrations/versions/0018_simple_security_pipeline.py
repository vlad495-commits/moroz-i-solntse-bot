"""Align security eval source with the always-on LLM boundary."""

from alembic import op


revision = "0018_simple_security"
down_revision = "0017_llm_compact"
branch_labels = None
depends_on = None

_CASE_KEYS = (
    "security-fp-01",
    "security-fp-02",
    "security-fp-03",
    "security-fp-04",
    "security-fp-05",
    "security-fp-06",
)


def _set_source(source: str) -> None:
    quoted_keys = ", ".join(f"'{key}'" for key in _CASE_KEYS)
    op.execute(
        "UPDATE eval_cases "
        f"SET expected_data = jsonb_set(expected_data, '{{source}}', '\"{source}\"') "
        "WHERE suite = 'security' "
        f"AND case_key IN ({quoted_keys})"
    )


def upgrade() -> None:
    _set_source("llm")


def downgrade() -> None:
    _set_source("local")
