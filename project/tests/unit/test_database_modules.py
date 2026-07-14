import asyncio
import importlib

import pytest


bot_database = importlib.import_module("db")
admin_database = importlib.import_module("database")

pytestmark = pytest.mark.asyncio


class SpyDatabase:
    instances = []

    def __init__(self, database_url, **kwargs):
        self.database_url = database_url
        self.kwargs = kwargs
        self.closed = False
        type(self).instances.append(self)

    async def connect(self):
        pass

    async def close(self):
        self.closed = True


async def test_bot_init_preserves_pool_sizes(monkeypatch):
    SpyDatabase.instances = []
    monkeypatch.setattr(bot_database, "DATABASE_URL", "database-url")
    monkeypatch.setattr(bot_database, "Database", SpyDatabase)
    monkeypatch.setattr(bot_database, "_pool", None)
    monkeypatch.setattr(bot_database, "_pool_lock", asyncio.Lock(), raising=False)

    await bot_database.init_db()

    assert len(SpyDatabase.instances) == 1
    assert SpyDatabase.instances[0].kwargs == {"min_size": 2, "max_size": 10}


async def test_admin_init_preserves_pool_sizes(monkeypatch):
    SpyDatabase.instances = []
    monkeypatch.setattr(admin_database, "DATABASE_URL", "database-url")
    monkeypatch.setattr(admin_database, "Database", SpyDatabase)
    monkeypatch.setattr(admin_database, "_pool", None)

    await admin_database.init_db()

    assert len(SpyDatabase.instances) == 1
    assert SpyDatabase.instances[0].kwargs == {"min_size": 1, "max_size": 5}


async def test_bot_concurrent_reconnect_creates_one_wrapper(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    instances = []

    class BlockingDatabase(SpyDatabase):
        def __init__(self, database_url, **kwargs):
            super().__init__(database_url, **kwargs)
            instances.append(self)

        async def connect(self):
            started.set()
            await release.wait()

    monkeypatch.setattr(bot_database, "DATABASE_URL", "database-url")
    monkeypatch.setattr(bot_database, "Database", BlockingDatabase)
    monkeypatch.setattr(bot_database, "_pool", None)
    monkeypatch.setattr(bot_database, "_pool_lock", asyncio.Lock(), raising=False)

    first = asyncio.create_task(bot_database._ensure_pool())
    await started.wait()
    second = asyncio.create_task(bot_database._ensure_pool())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)

    assert results == [True, True]
    assert len(instances) == 1
    assert bot_database._pool is instances[0]


async def test_bot_failed_candidate_cannot_erase_working_pool(monkeypatch):
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    first_returned = asyncio.Event()
    instances = []

    class OneSuccessThenFailure(SpyDatabase):
        def __init__(self, database_url, **kwargs):
            super().__init__(database_url, **kwargs)
            self.index = len(instances)
            instances.append(self)

        async def connect(self):
            if self.index == 0:
                first_started.set()
                await release_first.wait()
                first_returned.set()
                return
            await first_returned.wait()
            raise RuntimeError("expected concurrent failure")

    monkeypatch.setattr(bot_database, "DATABASE_URL", "database-url")
    monkeypatch.setattr(bot_database, "Database", OneSuccessThenFailure)
    monkeypatch.setattr(bot_database, "_pool", None)
    monkeypatch.setattr(bot_database, "_pool_lock", asyncio.Lock(), raising=False)

    first = asyncio.create_task(bot_database._ensure_pool())
    await first_started.wait()
    second = asyncio.create_task(bot_database._ensure_pool())
    await asyncio.sleep(0)
    release_first.set()
    results = await asyncio.gather(first, second)

    assert results == [True, True]
    assert len(instances) == 1
    assert bot_database._pool is instances[0]
