from pathlib import Path


PROJECT_ROOT = Path("/workspace")
if not PROJECT_ROOT.exists():
    PROJECT_ROOT = Path(__file__).resolve().parents[3]


def read(name: str) -> str:
    return (PROJECT_ROOT / "ops" / name).read_text(encoding="utf-8")


def test_deploy_runbook_has_exact_local_release_commands():
    doc = read("deploy-runbook.md")

    assert "cd /opt/moroz-i-solntse-bot/project" in doc
    assert "git pull --ff-only" in doc
    assert "docker compose --env-file ../.env -f docker-compose.yml -f docker-compose.prod.yml" in doc
    assert "--profile ops run --rm ops-check" in doc
    assert "alembic" in doc
    assert "smoke.ps1" in doc
    assert "No push or merge" in doc


def test_rollback_runbook_forbids_destructive_downgrade_without_backup():
    doc = read("rollback-runbook.md")
    compose = (PROJECT_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")

    assert "previous image" in doc
    assert "destructive downgrade" in doc
    assert "backup" in doc
    assert "restore-postgres.sh" in doc
    assert "exec -e RESTORE_TARGET_DB=moroz_restore postgres" in doc
    assert "forward-only" in doc
    for variable in ("BOT_IMAGE", "WORKER_IMAGE", "SCHEDULER_IMAGE", "ADMIN_IMAGE"):
        assert f"{variable}=" in doc
        assert f"${{{variable}:" in compose
    assert "up -d --no-build bot worker scheduler admin" in doc
    assert "docker image tag" not in doc


def test_incident_runbook_splits_technical_and_business_actions():
    doc = read("incident-runbook.md")

    assert "technical owner" in doc
    assert "business owner" in doc
    assert "PII" in doc
    assert "YCLIENTS" in doc
    assert "Telegram" in doc


def test_launch_checklist_names_blocking_evidence():
    doc = read("launch-checklist.md")

    for item in (
        "TLS",
        "rotated secrets",
        "TOTP",
        "YCLIENTS",
        "restore drill",
        "alerts",
        "evals",
        "load",
        "staff",
        "legal texts",
    ):
        assert item in doc
    assert "- [ ]" in doc
