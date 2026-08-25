import importlib.util
import json
from pathlib import Path


MIGRATION = Path("/workspace/migrations/versions/0014_llm_router_evaluations.py")
DATASET = Path("/workspace/llm/eval/router_dataset.json")


def test_migration_is_additive_and_uses_common_eval_tables():
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'down_revision = "0013_remove_eval_case_reviews"' in text
    assert 'op.add_column("eval_cases"' in text
    assert 'op.add_column("eval_runs"' in text
    assert 'op.add_column("eval_results"' in text
    assert 'op.add_column("token_usage"' in text
    assert "router_eval_cases" not in text
    assert (
        "op.drop_table"
        not in text.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    )


def test_migration_seed_matches_versioned_dataset():
    spec = importlib.util.spec_from_file_location("migration_0014", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert module.ROUTER_CASES == json.loads(DATASET.read_text(encoding="utf-8"))
