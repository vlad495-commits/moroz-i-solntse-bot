import asyncio
import logging
import os
import signal

from moroz.common.config import Settings
from moroz.common.queue import QueueTask, RabbitQueue


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")


async def handle(task: QueueTask) -> None:
    logger.error("No worker task handler is registered; task will be retried")
    raise NotImplementedError("No worker task handlers are registered")


async def _supervise(queue: RabbitQueue, stop: asyncio.Event) -> None:
    consumer = asyncio.create_task(queue.consume(handle))
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {consumer, waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if consumer in done:
            await consumer
            raise RuntimeError("Consumer stopped unexpectedly")
    finally:
        consumer.cancel()
        waiter.cancel()
        await asyncio.gather(consumer, waiter, return_exceptions=True)
        await queue.close()


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    queue = RabbitQueue(Settings.from_env(os.environ).rabbitmq_url)
    await queue.connect()
    logger.info("Worker started")
    try:
        await _supervise(queue, stop)
    finally:
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
