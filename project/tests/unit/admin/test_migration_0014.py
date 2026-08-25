import importlib.util
import hashlib
import json
from pathlib import Path

import pytest


MIGRATION = Path("/workspace/migrations/versions/0014_llm_router_evaluations.py")
DATASET = Path("/workspace/llm/eval/router_dataset.json")
DATASET_SHA256 = "87c8eb45783c44d7760d0ac2c69b957325fa3a22490d1551c0991fe620004f84"


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


def test_migration_pins_dataset_bytes_and_rejects_tampering(tmp_path):
    spec = importlib.util.spec_from_file_location("migration_0014_hash", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert hashlib.sha256(DATASET.read_bytes()).hexdigest() == DATASET_SHA256
    assert module.ROUTER_DATASET_SHA256 == DATASET_SHA256

    tampered = tmp_path / "router_dataset.json"
    tampered.write_bytes(DATASET.read_bytes() + b" ")
    with pytest.raises(RuntimeError, match="Router dataset integrity mismatch"):
        module._load_router_cases(tampered)


def test_migration_dataset_hash_requires_lf_checkout_bytes():
    data = DATASET.read_bytes()

    assert b"\r\n" not in data
    assert hashlib.sha256(data).hexdigest() == DATASET_SHA256
    assert hashlib.sha256(data.replace(b"\n", b"\r\n")).hexdigest() != DATASET_SHA256
