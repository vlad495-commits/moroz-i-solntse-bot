import asyncio
import importlib.util
import os
from pathlib import Path
from types import MappingProxyType
from uuid import uuid4

import pytest
from moroz.notifications.models import SchedulerJob


MODULE_PATH = Path("/workspace/scheduler/main.py")


def load_scheduler():
    spec = importlib.util.spec_from_file_location("scheduler_main", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scheduler_rejects_stale_heartbeat(tmp_path):
    scheduler = load_scheduler()
    heartbeat = tmp_path / "heartbeat"
    heartbeat.touch()
    os.utime(heartbeat, (100, 100))

    assert scheduler.heartbeat_is_fresh(heartbeat, max_age=75, now=174) is True
    assert scheduler.heartbeat_is_fresh(heartbeat, max_age=75, now=176) is False


@pytest.mark.asyncio
async def test_scheduler_updates_and_removes_heartbeat(tmp_path):
    scheduler = load_scheduler()
    heartbeat = tmp_path / "heartbeat"
    stop = asyncio.Event()
    loop = asyncio.create_task(
        scheduler.run_loop(stop, heartbeat_path=heartbeat, interval=0.01)
    )

    for _ in range(100):
        if heartbeat.exists():
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("heartbeat was not created")

    stop.set()
    await loop

    assert not heartbeat.exists()


@pytest.mark.asyncio
async def test_scheduler_pump_publishes_claimed_jobs():
    scheduler = load_scheduler()

    class Repository:
        def __init__(self):
            self.claims = []
            self.job_id = uuid4()

        async def claim_due(self, *, limit):
            self.claims.append(limit)
            return [
                SchedulerJob(
                    id=self.job_id,
                    kind="booking_created",
                    run_at=None,
                    payload=MappingProxyType({"booking_key": "booking-1"}),
                    idempotency_key="booking:1:v1:booking_created",
                    attempts=0,
                    booking_key=None,
                    booking_starts_at=None,
                )
            ]

    class Queue:
        def __init__(self):
            self.tasks = []

        async def publish(self, task):
            self.tasks.append(task)

    repository = Repository()
    queue = Queue()

    assert await scheduler.SchedulerPump(repository, queue, limit=25).run_once() == 1

    assert repository.claims == [25]
    assert queue.tasks[0].kind == "scheduler_job"
    assert queue.tasks[0].payload == {"job_id": str(repository.job_id)}
    assert queue.tasks[0].idempotency_key == f"scheduler_job:{repository.job_id}"


@pytest.mark.asyncio
async def test_scheduler_pump_releases_claim_when_publish_fails():
    scheduler = load_scheduler()

    class Repository:
        def __init__(self):
            self.job_ids = [uuid4(), uuid4()]
            self.released = []

        async def claim_due(self, *, limit):
            return [
                SchedulerJob(
                    id=job_id,
                    kind="booking_created",
                    run_at=None,
                    payload=MappingProxyType({}),
                    idempotency_key=f"booking:{job_id}:booking_created",
                    attempts=0,
                    booking_key=None,
                    booking_starts_at=None,
                )
                for job_id in self.job_ids
            ]

        async def release_claim(self, job_id):
            self.released.append(job_id)

    class FailingQueue:
        async def publish(self, _task):
            raise RuntimeError("broker down")

    repository = Repository()

    with pytest.raises(RuntimeError, match="broker down"):
        await scheduler.SchedulerPump(repository, FailingQueue()).run_once()

    assert repository.released == repository.job_ids


@pytest.mark.asyncio
async def test_scheduler_run_wires_runtime_resources(monkeypatch):
    scheduler = load_scheduler()
    closed = []
    captured = {}

    class RuntimeDatabase:
        def __init__(self, url, **kwargs):
            self.url = url

        async def connect(self):
            captured["database_connected"] = self.url

        async def close(self):
            closed.append("database")

    class RuntimeQueue:
        def __init__(self, url):
            self.url = url

        async def connect(self):
            captured["queue_connected"] = self.url

        async def close(self):
            closed.append("queue")

    async def run_loop(stop, **kwargs):
        captured["pump"] = kwargs["pump"]
        stop.set()

    monkeypatch.setenv("DATABASE_URL", "postgresql://local")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://local")
    monkeypatch.setattr(scheduler, "Database", RuntimeDatabase)
    monkeypatch.setattr(scheduler, "RabbitQueue", RuntimeQueue)
    monkeypatch.setattr(scheduler, "run_loop", run_loop)

    await scheduler.run()

    assert captured["database_connected"] == "postgresql://local"
    assert captured["queue_connected"] == "amqp://local"
    assert isinstance(captured["pump"], scheduler.SchedulerPump)
    assert set(closed) == {"database", "queue"}
