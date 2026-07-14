from contextlib import AbstractAsyncContextManager

import asyncpg


class Database:
    def __init__(self, database_url: str):
        self._database_url = database_url
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self._database_url)

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    def acquire(self) -> AbstractAsyncContextManager[asyncpg.Connection]:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        return self._pool.acquire()
