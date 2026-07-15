import re
from pathlib import Path


REPO = Path("/repo")
DOCUMENTS = [
    REPO / "AGENTS.md",
    REPO / "План реализации.md",
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
