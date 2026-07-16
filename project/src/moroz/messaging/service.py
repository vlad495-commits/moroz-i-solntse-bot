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
        if not await self._repository.accept(message):
            return False
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
