from redis.exceptions import RedisError

from moroz.common.db import Database
from moroz.messaging.buffer import MessageBuffer
from moroz.messaging.models import IncomingMessage
from moroz.messaging.outbox import enqueue_process_message
from moroz.messaging.repository import MessageRepository


class MessageService:
    def __init__(
        self,
        repository: MessageRepository,
        buffer: MessageBuffer,
        database: Database,
    ):
        self._repository = repository
        self._buffer = buffer
        self._database = database

    async def accept(self, message: IncomingMessage) -> bool:
        return await self._accept(message, require_consent=False)

    async def accept_consented(self, message: IncomingMessage) -> bool:
        return await self._accept(message, require_consent=True)

    async def _accept(
        self, message: IncomingMessage, *, require_consent: bool
    ) -> bool:
        if not require_consent:
            if not await self._repository.accept(message):
                return False
            return await self._buffer_or_enqueue(message)

        try:
            lock = self._buffer.lock(message.chat_id)
            if not await lock.acquire():
                return await self._repository.accept_if_consented(
                    message,
                    enqueue_directly=True,
                )
        except RedisError:
            return await self._repository.accept_if_consented(
                message,
                enqueue_directly=True,
            )
        try:
            if not await self._repository.accept_if_consented(message):
                return False
            await self._buffer.append_locked(
                message.chat_id,
                message.update_id,
                message.text,
            )
            return True
        finally:
            await lock.release()

    async def _buffer_or_enqueue(self, message: IncomingMessage) -> bool:
        try:
            await self._buffer.append(
                message.chat_id,
                message.update_id,
                message.text,
            )
        except RedisError:
            await enqueue_process_message(
                self._database,
                chat_id=message.chat_id,
                update_ids=(message.update_id,),
            )
        return True
