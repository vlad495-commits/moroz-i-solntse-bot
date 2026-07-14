import asyncio

import asyncpg
import pytest

from moroz.common.db import Database


pytestmark = pytest.mark.asyncio


class FakePool:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_concurrent_connect_creates_and_closes_one_pool(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    pools = []

    async def create_pool(*args, **kwargs):
        pool = FakePool()
        pools.append(pool)
        started.set()
        await release.wait()
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    database = Database("database-url")

    first = asyncio.create_task(database.connect())
    await started.wait()
    second = asyncio.create_task(database.connect())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    await database.close()

    assert len(pools) == 1
    assert pools[0].closed


async def test_close_waiting_for_connect_leaves_database_disconnected(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    pool = FakePool()

    async def create_pool(*args, **kwargs):
        started.set()
        await release.wait()
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    database = Database("database-url")

    connect = asyncio.create_task(database.connect())
    await started.wait()
    close = asyncio.create_task(database.close())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(connect, close)

    assert pool.closed
    with pytest.raises(RuntimeError, match="Database is not connected"):
        database.acquire()


async def test_connect_passes_requested_pool_sizes(monkeypatch):
    calls = []
    pool = FakePool()

    async def create_pool(*args, **kwargs):
        calls.append((args, kwargs))
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    database = Database("database-url", min_size=2, max_size=5)

    await database.connect()
    await database.close()

    assert calls == [(("database-url",), {"min_size": 2, "max_size": 5})]
