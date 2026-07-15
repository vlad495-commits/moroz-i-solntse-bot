import asyncio
import logging
import os
import signal
from pathlib import Path

from moroz.common.queue import QueueTask, RabbitQueue


logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("worker")
READINESS_PATH = Path("/tmp/worker-ready")


async def handle(task: QueueTask) -> None:
    logger.error("No worker task handler is registered; task will be retried")
    raise NotImplementedError("No worker task handlers are registered")


def _remove_readiness(path: Path) -> None:
    path.unlink(missing_ok=True)


def _publish_readiness(path: Path, active: bool) -> None:
    if active:
        path.write_text("ready", encoding="utf-8")
    else:
        _remove_readiness(path)


async def _supervise(
    queue: RabbitQueue,
    stop: asyncio.Event,
    readiness_path: Path = READINESS_PATH,
) -> None:
    _remove_readiness(readiness_path)
    consumer = asyncio.create_task(
        queue.consume(
            handle,
            readiness=lambda active: _publish_readiness(readiness_path, active),
        )
    )
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
        _remove_readiness(readiness_path)
        consumer.cancel()
        waiter.cancel()
        await asyncio.gather(consumer, waiter, return_exceptions=True)
        await queue.close()


async def run() -> None:
    _remove_readiness(READINESS_PATH)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    queue = RabbitQueue(os.environ["RABBITMQ_URL"])
    await queue.connect()
    logger.info("Worker started")
    try:
        await _supervise(queue, stop)
    finally:
        _remove_readiness(READINESS_PATH)
        logger.info("Worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
