import pytest

from moroz.common.db import Database


pytestmark = pytest.mark.asyncio


async def test_database_connect_acquire_and_close(migrated_database_url):
    database = Database(migrated_database_url)
    await database.connect()
    try:
        async with database.acquire() as connection:
            assert await connection.fetchval("SELECT 1") == 1
    finally:
        await database.close()


async def test_database_acquire_before_connect_fails_clearly(
    disposable_database_url,
):
    database = Database(disposable_database_url)

    with pytest.raises(RuntimeError, match="Database is not connected"):
        async with database.acquire():
            pass


async def test_database_close_is_idempotent_and_disconnects(
    disposable_database_url,
):
    database = Database(disposable_database_url)
    await database.close()
    await database.connect()
    await database.close()
    await database.close()

    with pytest.raises(RuntimeError, match="Database is not connected"):
        async with database.acquire():
            pass
