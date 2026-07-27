import asyncio
import logging
import os
import signal
import time
from pathlib import Path

from moroz.common.queue import QueueTask


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("scheduler")
HEARTBEAT_PATH = Path("/tmp/scheduler-heartbeat")
HEARTBEAT_INTERVAL = 30.0
CLAIM_LIMIT = 100


class SchedulerPump:
    def __init__(self, repository, queue, *, limit: int = CLAIM_LIMIT):
        self._repository = repository
        self._queue = queue
        self._limit = limit

    async def run_once(self) -> int:
        jobs = await self._repository.claim_due(limit=self._limit)
        for job in jobs:
            await self._queue.publish(
                QueueTask(
                    kind="scheduler_job",
                    payload={"job_id": str(job.id)},
                    idempotency_key=f"scheduler_job:{job.id}",
                )
            )
        return len(jobs)


def heartbeat_is_fresh(
    path: Path = HEARTBEAT_PATH,
    *,
    max_age: float = 75,
    now: float | None = None,
) -> bool:
    try:
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return False
    return (time.time() if now is None else now) - modified <= max_age


async def run_loop(
    stop: asyncio.Event,
    *,
    heartbeat_path: Path = HEARTBEAT_PATH,
    interval: float = HEARTBEAT_INTERVAL,
    pump: SchedulerPump | None = None,
) -> None:
    heartbeat_path.unlink(missing_ok=True)
    try:
        while not stop.is_set():
            heartbeat_path.touch()
            logger.info("Scheduler heartbeat")
            if pump is not None:
                published = await pump.run_once()
                if published:
                    logger.info("Scheduler published jobs count=%d", published)
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
    finally:
        heartbeat_path.unlink(missing_ok=True)


async def run() -> None:
    HEARTBEAT_PATH.unlink(missing_ok=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(name, stop.set)

    logger.info("Scheduler started")
    try:
        await run_loop(stop)
    finally:
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    asyncio.run(run())
