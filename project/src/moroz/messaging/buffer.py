import json
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.exceptions import LockError

from moroz.common.db import Database
from moroz.messaging.outbox import enqueue_process_message


BUFFER_SECONDS = 5
BUFFER_TTL_SECONDS = 30
DEADLINE_INDEX_KEY = "buffer:deadlines"


@dataclass(frozen=True, slots=True)
class BufferedMessage:
    chat_id: str
    update_ids: tuple[str, ...]
    text: str


class MessageBuffer:
    def __init__(self, redis, database: Database, *, clock=None):
        self._redis = redis
        self._database = database
        self._now = clock.now if clock is not None else lambda: datetime.now(UTC)

    async def append(self, chat_id: str, update_id: str, text: str) -> None:
        key = f"buffer:{chat_id}"
        lock = self._redis.lock(
            f"lock:{key}", timeout=BUFFER_TTL_SECONDS, blocking_timeout=1
        )
        if not await lock.acquire():
            raise LockError(f"Could not acquire {key} append lock")
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.rpush(
                    key,
                    json.dumps(
                        {"update_id": update_id, "text": text},
                        ensure_ascii=False,
                    ),
                )
                pipe.expire(key, BUFFER_TTL_SECONDS)
                pipe.zadd(
                    DEADLINE_INDEX_KEY,
                    {chat_id: self._now().timestamp() + BUFFER_SECONDS},
                )
                await pipe.execute()
        finally:
            await lock.release()

    async def due_chat_ids(self, limit: int = 100) -> tuple[str, ...]:
        if limit <= 0:
            return ()
        return tuple(
            await self._redis.zrangebyscore(
                DEADLINE_INDEX_KEY,
                "-inf",
                self._now().timestamp(),
                start=0,
                num=limit,
            )
        )

    async def flush(self, chat_id: str) -> BufferedMessage | None:
        key = f"buffer:{chat_id}"
        lock = self._redis.lock(
            f"lock:{key}", timeout=BUFFER_TTL_SECONDS, blocking_timeout=1
        )
        if not await lock.acquire():
            return None
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.lrange(key, 0, -1)
                pipe.zscore(DEADLINE_INDEX_KEY, chat_id)
                entries, deadline = await pipe.execute()
            if not entries:
                await self._redis.zrem(DEADLINE_INDEX_KEY, chat_id)
                return None
            if deadline is None:
                return None
            if self._now().timestamp() < float(deadline):
                return None

            decoded = [json.loads(entry) for entry in entries]
            buffered = BufferedMessage(
                chat_id=chat_id,
                update_ids=tuple(entry["update_id"] for entry in decoded),
                text="\n".join(entry["text"] for entry in decoded),
            )
            await enqueue_process_message(
                self._database,
                chat_id=buffered.chat_id,
                update_ids=buffered.update_ids,
                text=buffered.text,
            )
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.delete(key)
                pipe.zrem(DEADLINE_INDEX_KEY, chat_id)
                await pipe.execute()
            return buffered
        finally:
            await lock.release()
