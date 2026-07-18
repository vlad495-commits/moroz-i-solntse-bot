import re
from pathlib import Path


REPO = Path("/repo")
DOCUMENTS = [
    REPO / "AGENTS.md",
    REPO / "План реализации.md",
    Path("/workspace/ops/staging-runbook.md"),
    *sorted((REPO / "docs/superpowers/plans").glob("*.md")),
]


def test_official_compose_commands_use_parent_env_file():
    for path in DOCUMENTS:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"docker compose(?! --env-file \.\./\.env)", text), path


def test_release_checkpoints_do_not_start_telegram_polling():
    master = (REPO / "План реализации.md").read_text(encoding="utf-8")
    telegram = (
        REPO / "docs/superpowers/plans/2026-07-14-production-v1-telegram-pipeline.md"
    ).read_text(encoding="utf-8")

    for text in (master, telegram):
        assert "docker compose --env-file ../.env up -d --build &&" not in text
        assert (
            "docker compose --env-file ../.env up -d --build "
            "postgres redis rabbitmq admin worker scheduler"
        ) in text
        assert "--entrypoint python bot -m compileall -q /app" in text
        assert (
            "--entrypoint python bot -c "
            "\"import cache, config, db, handlers, llm\""
        ) in text


def test_canonical_full_docker_gates_build_test_image_before_pytest():
    master = (REPO / "План реализации.md").read_text(encoding="utf-8")
    foundation = (
        REPO / "docs/superpowers/plans/2026-07-14-production-v1-foundation.md"
    ).read_text(encoding="utf-8")
    build = "docker compose --env-file ../.env --profile test build test"
    run = "docker compose --env-file ../.env --profile test run --rm test pytest -q"

    assert master.index(build) < master.index(run)
    task_5 = foundation.split("### Task 5:", 1)[1].split("### Task 6:", 1)[0]
    assert task_5.index(build) < task_5.index(run)


def test_first_agents_docker_command_runs_from_project_directory():
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    first_command = re.search(r"```bash\s*(?:#[^\n]*\n)?([^\n]+)", agents).group(1)

    assert first_command.startswith("cd project && docker compose ")
