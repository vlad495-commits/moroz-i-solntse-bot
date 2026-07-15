import asyncio
from contextlib import AbstractAsyncContextManager

import asyncpg


class Database:
    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 10,
        max_size: int = 10,
    ):
        self._database_url = database_url
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None
        self._lifecycle_lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lifecycle_lock:
            if self._pool is None:
                self._pool = await asyncpg.create_pool(
                    self._database_url,
                    min_size=self._min_size,
                    max_size=self._max_size,
                )

    async def close(self) -> None:
        async with self._lifecycle_lock:
            pool, self._pool = self._pool, None
            if pool is not None:
                await pool.close()

    def acquire(self) -> AbstractAsyncContextManager[asyncpg.Connection]:
        if self._pool is None:
            raise RuntimeError("Database is not connected")
        return self._pool.acquire()
