from pathlib import Path

import yaml


def test_migration_commands_are_excluded_from_default_profile():
    services = yaml.safe_load(
        Path("/workspace/docker-compose.yml").read_text(encoding="utf-8")
    )["services"]

    assert services["migrate"]["profiles"] == ["migration"]
    assert services["cutover"]["profiles"] == ["migration"]
