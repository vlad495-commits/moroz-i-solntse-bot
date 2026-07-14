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
    logger.info(
        "Task received kind=%s idempotency_key=%s",
        task.kind,
        task.idempotency_key,
    )


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    queue = RabbitQueue(Settings.from_env(os.environ).rabbitmq_url)
    await queue.connect()
    consumer = asyncio.create_task(queue.consume(handle))
    logger.info("Worker started")
    try:
        await stop.wait()
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
        await queue.close()
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
