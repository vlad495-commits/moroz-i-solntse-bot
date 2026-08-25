import asyncio

import pytest

import app as admin_app
import eval_routes
import eval_runner


@pytest.mark.asyncio
@pytest.mark.parametrize("suite", ["answer", "router"])
async def test_cancelled_eval_run_persists_safe_terminal_status(
    monkeypatch,
    suite,
    caplog,
):
    sentinel = "https://user:password@provider.invalid cancellation-private-sentinel"
    started = asyncio.Event()
    finished = []

    async def blocked_case(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    async def finish_run(*args, **kwargs):
        finished.append((args, kwargs))

    monkeypatch.setattr(eval_runner.evdb, "finish_run", finish_run)
    if suite == "answer":
        monkeypatch.setattr(eval_runner, "_init_clients", lambda: None)
        monkeypatch.setattr(eval_runner, "run_case", blocked_case)
        coroutine = eval_runner.run_eval_set(71, cases=[{"category": "general"}])
    else:
        monkeypatch.setattr(eval_runner, "run_router_case", blocked_case)
        coroutine = eval_runner.run_router_eval_set(
            72,
            cases=[{"category": "general", "critical": False}],
            router=object(),
        )

    task = asyncio.create_task(coroutine)
    await started.wait()
    task.cancel(sentinel)

    with pytest.raises(asyncio.CancelledError):
        await task

    run_id = 71 if suite == "answer" else 72
    assert finished == [
        (
            (run_id, 0, 0),
            {"status": "error", "error_message": "CancelledError"},
        )
    ]
    assert sentinel not in caplog.text
    assert sentinel not in repr(finished)


@pytest.mark.asyncio
async def test_cancel_eval_tasks_cancels_drains_and_retrieves_owned_tasks():
    cancelled = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    exception_contexts = []
    loop.set_exception_handler(
        lambda _loop, context: exception_contexts.append(context)
    )

    async def blocked(name):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(name)
            raise

    try:
        eval_routes._start_eval_task(81, blocked("answer"))
        eval_routes._start_eval_task(82, blocked("router"))
        await asyncio.sleep(0)

        await eval_routes.cancel_eval_tasks()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert sorted(cancelled) == ["answer", "router"]
    assert eval_routes._eval_tasks == set()
    assert exception_contexts == []


@pytest.mark.asyncio
async def test_admin_shutdown_drains_eval_tasks_before_database_close(monkeypatch):
    events = []

    async def init_db():
        events.append("init")

    async def cancel_eval_tasks():
        events.append("drain")

    async def close_db():
        events.append("close")

    monkeypatch.setattr(admin_app.database, "init_db", init_db)
    monkeypatch.setattr(admin_app, "cancel_eval_tasks", cancel_eval_tasks)
    monkeypatch.setattr(admin_app.database, "close_db", close_db)

    async with admin_app.lifespan(admin_app.app):
        events.append("serve")

    assert events == ["init", "serve", "drain", "close"]
