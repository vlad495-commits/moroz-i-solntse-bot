from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


MIGRATION = Path("/workspace/migrations/versions/0017_llm_compact_evaluations.py")
DATASET = Path("/workspace/llm/eval/compact_dataset.json")
MIGRATE_DOCKERFILE = Path("/workspace/migrate/Dockerfile")


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0017", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migration_is_additive_and_uses_common_eval_tables():
    text = MIGRATION.read_text(encoding="utf-8")
    module = _load_migration()

    assert 'down_revision = "0016_llm_validator"' in text
    assert module.revision == "0017_llm_compact"
    assert len(module.revision) <= 32
    assert '"suite": "compact"' in text
    assert "compact_eval_cases" not in text
    assert "op.add_column" not in text
    assert "op.create_table" not in text
    assert "op.drop_table" not in text


def test_migration_seed_matches_versioned_dataset():
    module = _load_migration()

    assert module.COMPACT_CASES == json.loads(DATASET.read_text(encoding="utf-8"))


def test_migration_pins_dataset_bytes_and_rejects_tampering(tmp_path):
    module = _load_migration()
    canonical = DATASET.read_bytes().replace(b"\r\n", b"\n")

    assert module.COMPACT_DATASET_SHA256 == hashlib.sha256(canonical).hexdigest()
    tampered = tmp_path / "compact_dataset.json"
    tampered.write_bytes(DATASET.read_bytes() + b" ")
    with pytest.raises(RuntimeError, match="Compact dataset integrity mismatch"):
        module._load_compact_cases(tampered)


def test_migration_dataset_hash_accepts_git_line_ending_conversion(tmp_path):
    module = _load_migration()
    canonical = DATASET.read_bytes().replace(b"\r\n", b"\n")
    crlf_dataset = tmp_path / "compact_dataset.json"
    crlf_dataset.write_bytes(canonical.replace(b"\n", b"\r\n"))

    assert module._load_compact_cases(crlf_dataset) == json.loads(canonical)


def test_downgrade_targets_only_compact_owned_rows():
    text = MIGRATION.read_text(encoding="utf-8")
    downgrade = text.split("def downgrade", 1)[1]

    assert "suite = 'compact'" in downgrade
    assert "suite = 'validator'" not in downgrade
    assert "suite = 'security'" not in downgrade
    assert "suite = 'router'" not in downgrade
    assert "suite = 'answer'" not in downgrade
    assert "OR case_id IN" not in downgrade


def test_migrate_image_copies_versioned_compact_dataset():
    text = MIGRATE_DOCKERFILE.read_text(encoding="utf-8")

    assert (
        "COPY llm/eval/compact_dataset.json "
        "/app/llm/eval/compact_dataset.json"
    ) in text
