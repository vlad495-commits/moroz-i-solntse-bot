import asyncio
import importlib.util
import os
from pathlib import Path

import pytest


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
