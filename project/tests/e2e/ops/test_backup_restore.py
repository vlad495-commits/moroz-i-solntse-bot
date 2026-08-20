import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path("/workspace")
if not PROJECT_ROOT.exists():
    PROJECT_ROOT = Path(__file__).resolve().parents[3]


def make_fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    for name in ("pg_dump", "createdb", "pg_restore", "psql"):
        (bin_dir / name).write_text(
            "#!/bin/sh\n"
            f"echo {name} \"$@\" >> \"{log}\"\n"
            "while [ $# -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-f\" ]; then shift; printf dump > \"$1\"; fi\n"
            "  shift\n"
            "done\n",
            encoding="utf-8",
        )
    (bin_dir / "openssl").write_text(
        "#!/bin/sh\n"
        f"echo openssl \"$@\" >> \"{log}\"\n"
        "in=''; out=''\n"
        "while [ $# -gt 0 ]; do\n"
        "  [ \"$1\" = \"-in\" ] && { shift; in=\"$1\"; }\n"
        "  [ \"$1\" = \"-out\" ] && { shift; out=\"$1\"; }\n"
        "  shift\n"
        "done\n"
        "if [ \"${FAKE_OPENSSL_FAIL:-}\" = \"1\" ]; then\n"
        "  printf partial > \"$out\"\n"
        "  exit 9\n"
        "fi\n"
        "cp \"$in\" \"$out\"\n",
        encoding="utf-8",
    )
    (bin_dir / "sha256sum").write_text(
        "#!/bin/sh\n"
        f"echo sha256sum \"$@\" >> \"{log}\"\n"
        "if [ \"$1\" = \"-c\" ]; then exit 0; fi\n"
        "printf 'abc123  %s\\n' \"$1\"\n",
        encoding="utf-8",
    )
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    return bin_dir


def base_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{make_fake_bin(tmp_path)}{os.pathsep}{env['PATH']}",
            "BACKUP_DIR": str(tmp_path / "backups"),
            "BACKUP_ENCRYPTION_KEY": "local-test-key",
            "POSTGRES_USER": "moroz",
            "POSTGRES_DB": "moroz",
            "PGPASSWORD": "postgres-password",
        }
    )
    return env


def test_backup_postgres_creates_encrypted_dump_and_checksum(tmp_path):
    result = subprocess.run(
        ["sh", str(PROJECT_ROOT / "ops" / "backup-postgres.sh")],
        env=base_env(tmp_path),
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    backups = list((tmp_path / "backups").glob("moroz-*.dump.enc"))
    assert len(backups) == 1
    assert backups[0].with_suffix(".enc.sha256").exists()
    commands = (tmp_path / "commands.log").read_text(encoding="utf-8")
    assert "pg_dump --format=custom --no-owner --no-acl" in commands
    assert "openssl enc -aes-256-cbc -pbkdf2 -salt" in commands
    assert "sha256sum" in commands


def test_backup_failure_removes_raw_and_partial_artifacts(tmp_path):
    env = base_env(tmp_path)
    env["FAKE_OPENSSL_FAIL"] = "1"

    result = subprocess.run(
        ["sh", str(PROJECT_ROOT / "ops" / "backup-postgres.sh")],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 9
    assert list((tmp_path / "backups").iterdir()) == []


def test_restore_postgres_refuses_to_restore_over_primary_database(tmp_path):
    env = base_env(tmp_path)
    env["RESTORE_TARGET_DB"] = "moroz"
    backup = tmp_path / "backup.dump.enc"
    backup.write_text("encrypted", encoding="utf-8")

    result = subprocess.run(
        ["sh", str(PROJECT_ROOT / "ops" / "restore-postgres.sh"), str(backup)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "RESTORE_TARGET_DB must differ from POSTGRES_DB" in result.stderr


def test_restore_postgres_restores_into_separate_database(tmp_path):
    env = base_env(tmp_path)
    env["RESTORE_TARGET_DB"] = "moroz_restore"
    backup = tmp_path / "backup.dump.enc"
    backup.write_text("encrypted", encoding="utf-8")

    result = subprocess.run(
        ["sh", str(PROJECT_ROOT / "ops" / "restore-postgres.sh"), str(backup)],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    commands = (tmp_path / "commands.log").read_text(encoding="utf-8")
    assert "openssl enc -d -aes-256-cbc -pbkdf2" in commands
    assert "createdb --username moroz moroz_restore" in commands
    assert (
        "pg_restore --clean --if-exists --no-owner --no-acl "
        "--username moroz --dbname moroz_restore" in commands
    )
    assert "psql --username moroz --dbname moroz_restore" in commands


def test_daily_backup_runner_rejects_invalid_interval():
    env = os.environ.copy()
    env["BACKUP_INTERVAL_SECONDS"] = "not-a-number"

    result = subprocess.run(
        ["sh", str(PROJECT_ROOT / "ops" / "run-backups.sh")],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "BACKUP_INTERVAL_SECONDS must be a positive integer" in result.stderr


def test_daily_backup_runner_executes_backup_then_sleeps():
    runner = (PROJECT_ROOT / "ops" / "run-backups.sh").read_text(encoding="utf-8")

    backup_call = runner.index('sh "$SCRIPT_DIR/backup-postgres.sh"')
    sleep_call = runner.index('sleep "$BACKUP_INTERVAL_SECONDS"')
    assert "BACKUP_INTERVAL_SECONDS=${BACKUP_INTERVAL_SECONDS:-86400}" in runner
    assert "while :" in runner
    assert backup_call < sleep_call


def test_backup_image_initializes_volume_then_drops_privileges():
    dockerfile = (PROJECT_ROOT / "ops" / "Dockerfile.backup").read_text(
        encoding="utf-8"
    )
    entrypoint = (PROJECT_ROOT / "ops" / "backup-entrypoint.sh").read_text(
        encoding="utf-8"
    )

    assert "USER postgres" not in dockerfile
    assert "backup-entrypoint.sh" in dockerfile
    assert 'BACKUP_DIR=/backups/postgres' in entrypoint
    assert 'chown -R postgres:postgres "$BACKUP_DIR"' in entrypoint
    assert "find /tmp" in entrypoint
    assert 'exec gosu postgres "$@"' in entrypoint


def test_backup_uses_unique_tmpfs_raw_and_partial_names():
    script = (PROJECT_ROOT / "ops" / "backup-postgres.sh").read_text(
        encoding="utf-8"
    )
    compose = (PROJECT_ROOT / "docker-compose.prod.yml").read_text(
        encoding="utf-8"
    )

    assert "mktemp /tmp/moroz-backup.dump.XXXXXX" in script
    assert "run_suffix=" in script
    assert "dump.enc.partial" in script
    assert "encrypted_local" not in script
    assert "/tmp:size=${BACKUP_TMPFS_SIZE:-1g},mode=0700,uid=70,gid=70" in compose
