import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


MIGRATION = Path("/workspace/migrations/versions/0019_router_v2.py")
DATASET = Path("/workspace/llm/eval/router_dataset_v2.json")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migration_is_data_only_and_owned_by_router_v2_suite():
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "0019_router_v2"' in text
    assert 'down_revision = "0018_simple_security"' in text
    assert '"suite": "router_v2"' in text
    assert '"expected_data": {"route": case["expected_route"]}' in text
    assert "op.add_column" not in text
    assert "op.create_table" not in text
    assert "UPDATE " not in text
    assert "suite = 'router'" not in text


def test_migration_seed_matches_versioned_dataset_and_pins_bytes(tmp_path):
    module = _load("migration_0019")
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))

    assert module.ROUTER_V2_CASES == dataset
    assert module.ROUTER_V2_DATASET_SHA256 == hashlib.sha256(
        DATASET.read_bytes()
    ).hexdigest()
    assert b"\r\n" not in DATASET.read_bytes()

    tampered = tmp_path / "router_dataset_v2.json"
    tampered.write_bytes(DATASET.read_bytes() + b" ")
    with pytest.raises(RuntimeError, match="Router v2 dataset integrity mismatch"):
        module._load_router_v2_cases(tampered)


def test_downgrade_deletes_only_v2_owned_rows_in_dependency_order():
    text = MIGRATION.read_text(encoding="utf-8")
    downgrade = text.split("def downgrade", 1)[1]

    result_delete = downgrade.index("DELETE FROM eval_results")
    run_delete = downgrade.index("DELETE FROM eval_runs")
    case_delete = downgrade.index("DELETE FROM eval_cases")
    assert result_delete < run_delete < case_delete
    assert downgrade.count("suite = 'router_v2'") >= 4
    assert "suite = 'router'" not in downgrade
