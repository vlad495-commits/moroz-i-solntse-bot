import asyncio

import pytest_asyncio

from moroz.booking.repository import BookingRepository
from moroz.common.db import Database
from tests.integration.conftest import (
    disposable_database_url,
    migrated_database_url,
)


__all__ = ["disposable_database_url", "migrated_database_url"]


@pytest_asyncio.fixture
async def repo(migrated_database_url):
    database = Database(migrated_database_url, min_size=1, max_size=1)
    await database.connect()
    try:
        yield BookingRepository(database, staff_chat_id="900001")
    finally:
        await database.close()


@pytest_asyncio.fixture
async def repo_pair(migrated_database_url):
    databases = [
        Database(migrated_database_url, min_size=1, max_size=1),
        Database(migrated_database_url, min_size=1, max_size=1),
    ]
    await asyncio.gather(*(database.connect() for database in databases))
    try:
        yield tuple(
            BookingRepository(database, staff_chat_id="900001")
            for database in databases
        )
    finally:
        await asyncio.gather(*(database.close() for database in databases))
