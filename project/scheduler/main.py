import asyncio
import logging
import os
import signal


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("scheduler")


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    logger.info("Scheduler started")
    try:
        while not stop.is_set():
            logger.info("Scheduler heartbeat")
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass
    finally:
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(run())
