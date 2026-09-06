import re
from pathlib import Path

import pytest


REPO = Path("/repo")
OWNERSHIP_PLAN = (
    REPO / "docs/superpowers/plans/2026-07-22-yclients-postgres-ownership.md"
)
AUDIT_PLAN = REPO / "docs/superpowers/plans/2026-09-06-reference-simplification.md"
DOCUMENTS = [
    REPO / "AGENTS.md",
    REPO / "План ручного тестирования.md",
    Path("/workspace/ops/staging-runbook.md"),
    *sorted((REPO / "docs/superpowers/plans").glob("*.md")),
]


def test_official_compose_commands_use_approved_env_file():
    for path in DOCUMENTS:
        text = path.read_text(encoding="utf-8")
        env_file = {
            OWNERSHIP_PLAN: "../tmp/compose-empty.env",
            AUDIT_PLAN: "../tmp/audit-test.env",
        }.get(path, "../.env")
        assert not re.search(
            rf"docker compose(?! --env-file {re.escape(env_file)}(?:\s|$))",
            text,
        ), path


@pytest.mark.parametrize("env_file", ["compose-empty.env", "audit-test.env"])
def test_normal_document_rejects_isolated_env(
    tmp_path: Path, monkeypatch, env_file: str,
) -> None:
    document = tmp_path / "staging-runbook.md"
    document.write_text(
        f"docker compose --env-file ../tmp/{env_file} up -d",
        encoding="utf-8",
    )
    monkeypatch.setitem(globals(), "DOCUMENTS", [document])

    with pytest.raises(AssertionError):
        test_official_compose_commands_use_approved_env_file()


def test_audit_plan_uses_isolated_project_and_override() -> None:
    commands = re.findall(
        r"^docker compose[^\n]*", AUDIT_PLAN.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    assert commands
    for command in commands:
        assert command.startswith("docker compose --env-file ../tmp/audit-test.env ")
        assert " -p moroz-reference-test " in command
        assert " -f docker-compose.yml -f docker-compose.audit-test.yml " in command


def test_ownership_plan_uses_empty_env_only_for_isolated_commands() -> None:
    commands = re.findall(
        r"^docker compose[^\n]*",
        OWNERSHIP_PLAN.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )

    assert commands
    assert all(
        command.startswith(
            "docker compose --env-file ../tmp/compose-empty.env "
        )
        for command in commands
    )


def test_first_agents_docker_command_runs_from_project_directory():
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    first_command = re.search(r"```bash\s*(?:#[^\n]*\n)?([^\n]+)", agents).group(1)

    assert first_command.startswith("cd project && docker compose ")
