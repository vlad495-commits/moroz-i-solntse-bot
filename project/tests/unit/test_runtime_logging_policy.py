import ast
import asyncio
import importlib
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import db as llm_db
import handlers
import logs_routes


RUNTIME_ROOTS = ("admin", "llm", "src", "worker", "scheduler")
PROJECT_ROOT = Path("/app")


class RuntimeSentinelError(RuntimeError):
    pass


def test_runtime_logging_policy_has_no_tracebacks_or_raw_exception_values():
    violations = []
    for root_name in RUNTIME_ROOTS:
        for path in (PROJECT_ROOT / root_name).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "logger.exception" in source or "exc_info=" in source:
                violations.append(f"{path}: traceback logging")
            tree = ast.parse(source)
            for handler in (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.ExceptHandler) and node.name
            ):
                for call in (
                    node for node in ast.walk(handler) if isinstance(node, ast.Call)
                ):
                    func = call.func
                    if not (
                        isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "logger"
                    ):
                        continue
                    for value in (*call.args[1:], *(kw.value for kw in call.keywords)):
                        rendered = ast.unparse(value)
                        references_error = any(
                            isinstance(node, ast.Name) and node.id == handler.name
                            for node in ast.walk(value)
                        )
                        if references_error and rendered != (
                            f"type({handler.name}).__name__"
                        ):
                            violations.append(
                                f"{path}:{call.lineno}: raw exception value"
                            )
    assert violations == []


@pytest.mark.asyncio
async def test_llm_handler_failure_log_is_safe(monkeypatch, caplog):
    sentinel = "https://user:password@provider handler-user-sentinel"
    answers = []

    class Message:
        chat = SimpleNamespace(id=81)
        from_user = SimpleNamespace(id=82, username="safe")
        text = "safe question"

        async def answer(self, text):
            answers.append(text)

    class Bot:
        async def send_chat_action(self, *_args):
            return None

    async def false():
        return False

    async def no_op(*_args, **_kwargs):
        return None

    async def empty_context(*_args, **_kwargs):
        return []

    async def fail_generate(*_args, **_kwargs):
        raise RuntimeSentinelError(sentinel)

    monkeypatch.setattr(handlers, "_is_bot_paused", false)
    monkeypatch.setattr(handlers.db, "save_message", no_op)
    monkeypatch.setattr(handlers.cache, "get_context", empty_context)
    monkeypatch.setattr(handlers.db, "get_context", empty_context)
    monkeypatch.setattr(handlers, "generate_response", fail_generate)

    with caplog.at_level(logging.ERROR, logger=handlers.logger.name):
        await handlers.handle_text(Message(), Bot())

    assert answers == ["Извините, временно не могу ответить. Попробуйте через минуту."]
    assert "llm_generate_failed chat_id=81 error_type=RuntimeSentinelError" in caplog.text
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_database_failure_log_is_safe(monkeypatch, caplog):
    sentinel = "postgresql://user:password@db database-user-sentinel"

    class Connection:
        async def execute(self, *_args):
            raise RuntimeSentinelError(sentinel)

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *_args):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    async def ready():
        return True

    monkeypatch.setattr(llm_db, "_ensure_pool", ready)
    monkeypatch.setattr(llm_db, "_pool", Pool())

    with caplog.at_level(logging.ERROR, logger=llm_db.logger.name):
        await llm_db.save_message(91, 92, "user", "safe content")

    assert "db_message_save_failed error_type=RuntimeSentinelError" in caplog.text
    assert sentinel not in caplog.text


def test_logs_route_failure_log_is_safe(caplog):
    sentinel = "C:/private/path-user-sentinel"

    class FailingPath:
        def exists(self):
            return True

        def open(self, *_args, **_kwargs):
            raise OSError(sentinel)

    with caplog.at_level(logging.ERROR, logger=logs_routes.logger.name):
        assert logs_routes._read_tail(FailingPath(), 10) == []

    assert "admin_log_read_failed error_type=OSError" in caplog.text
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_bot_polling_failure_is_retrieved_and_redacted(caplog):
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    bot_module = importlib.import_module("bot")
    root.handlers[:] = previous_handlers
    sentinel = "https://api:token@telegram polling-user-sentinel"
    loop = asyncio.get_running_loop()
    previous_exception_handler = loop.get_exception_handler()
    exception_contexts = []
    loop.set_exception_handler(lambda _loop, context: exception_contexts.append(context))

    async def fail():
        raise RuntimeSentinelError(sentinel)

    try:
        task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        reporter = getattr(bot_module, "_report_polling_task", None)
        with caplog.at_level(logging.ERROR, logger=bot_module.logger.name):
            if reporter is None:
                task.exception()
            else:
                reporter(task)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_exception_handler)

    assert reporter is not None
    assert "polling_failed error_type=RuntimeSentinelError" in caplog.text
    assert sentinel not in caplog.text
    assert exception_contexts == []
