"""Точка входа Telegram-бота. Запускается через: docker compose up llm."""

import asyncio
import logging
import os
import signal
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

import cache
import db
from config import (
    LOG_FILE,
    LOG_FILE_BACKUPS,
    LOG_FILE_MAX_BYTES,
    SHUTDOWN_INFLIGHT_TIMEOUT_SEC,
    TELEGRAM_BOT_TOKEN,
)
from handlers import router
from llm import init_llm, prompt_reload_listener


def _setup_logging() -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                LOG_FILE, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
        )
    except OSError:
        pass

    logging.basicConfig(format=fmt, level=logging.INFO, handlers=handlers, force=True)
    for name in ("httpx", "httpcore", "aiohttp.access", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


_setup_logging()
logger = logging.getLogger(__name__)


async def _cleanup_loop() -> None:
    """Раз в сутки удаляем записи старше DATA_RETENTION_DAYS."""
    while True:
        await asyncio.sleep(86400)
        try:
            stats = await db.cleanup_old_records()
            total = sum(stats.values()) if stats else 0
            if total > 0:
                logger.info("Автоочистка БД: удалено %d записей (%s)", total, stats)
        except Exception:
            logger.exception("Ошибка в _cleanup_loop")


async def _global_error_handler(event: ErrorEvent) -> bool:
    """Перехват необработанных ошибок aiogram."""
    exc = event.exception

    if isinstance(exc, TelegramForbiddenError):
        logger.info("Forbidden: %s", exc)
        return True
    if isinstance(exc, TelegramRetryAfter):
        logger.warning("Telegram RetryAfter: ждать %s сек", exc.retry_after)
        return True
    if isinstance(exc, TelegramNetworkError):
        logger.warning("Telegram NetworkError: %s", exc)
        return True
    if isinstance(exc, TelegramBadRequest):
        logger.error("Telegram BadRequest: %s", exc)
        return True

    logger.exception("Необработанная ошибка в aiogram", exc_info=exc)
    return True


async def _wait_for_shutdown(stop_event: asyncio.Event) -> None:
    """Ставит флаг при получении SIGTERM/SIGINT."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass
    await stop_event.wait()


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")

    await cache.init_cache()
    await db.init_db()
    init_llm()

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.errors.register(_global_error_handler)
    dp.include_router(router)

    me = await bot.get_me()
    logger.info("Бот запущен: @%s (id=%s)", me.username, me.id)

    background_tasks: list[asyncio.Task] = [
        asyncio.create_task(_cleanup_loop(), name="cleanup_loop"),
        asyncio.create_task(prompt_reload_listener(), name="prompt_reload_listener"),
    ]

    stop_event = asyncio.Event()
    polling_task = asyncio.create_task(
        dp.start_polling(bot, drop_pending_updates=True),
        name="aiogram_polling",
    )
    shutdown_task = asyncio.create_task(_wait_for_shutdown(stop_event))

    try:
        done, _ = await asyncio.wait(
            {polling_task, shutdown_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            if t is polling_task and t.exception():
                logger.exception("polling упал", exc_info=t.exception())
    finally:
        logger.info("Останавливаюсь, жду in-flight задачи до %d сек...", SHUTDOWN_INFLIGHT_TIMEOUT_SEC)
        await dp.stop_polling()

        inflight = [t for t in background_tasks if not t.done()]
        if inflight:
            done_inflight, pending = await asyncio.wait(
                inflight, timeout=SHUTDOWN_INFLIGHT_TIMEOUT_SEC,
            )
            for t in pending:
                t.cancel()
            for t in done_inflight | pending:
                with __import__("contextlib").suppress(BaseException):
                    await t

        await cache.close_cache()
        await db.close_db()
        await bot.session.close()
        logger.info("Бот остановлен, ресурсы освобождены")


if __name__ == "__main__":
    asyncio.run(main())
