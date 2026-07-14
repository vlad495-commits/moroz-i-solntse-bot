import os
import subprocess
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest_asyncio

from moroz.common.config import Settings


class RedactedDatabaseURL(str):
    def __repr__(self):
        return "'<redacted-database-url>'"


@pytest_asyncio.fixture
async def disposable_database_url():
    admin_url = Settings.from_env(os.environ).database_url
    assert admin_url
    database_name = f"test_migrations_{uuid.uuid4().hex}"
    parts = urlsplit(admin_url)
    test_url = RedactedDatabaseURL(
        urlunsplit(parts._replace(path=f"/{database_name}"))
    )

    admin = await asyncpg.connect(admin_url)
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    try:
        yield test_url
    finally:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()


@pytest_asyncio.fixture
async def migrated_database_url(disposable_database_url):
    subprocess.run(
        ["alembic", "-c", "/workspace/alembic.ini", "upgrade", "head"],
        check=True,
        env={**os.environ, "DATABASE_URL": disposable_database_url},
    )
    return disposable_database_url
