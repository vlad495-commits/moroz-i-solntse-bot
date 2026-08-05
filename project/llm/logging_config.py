"""Shared safe logging setup for every bot runtime mode."""

import logging
import os
from collections.abc import Callable
from logging.handlers import RotatingFileHandler

from config import LOG_FILE, LOG_FILE_BACKUPS, LOG_FILE_MAX_BYTES


def configure_logging(
    file_handler_factory: Callable[..., logging.Handler] = RotatingFileHandler,
) -> None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    file_error: OSError | None = None
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        handlers.append(
            file_handler_factory(
                LOG_FILE,
                maxBytes=LOG_FILE_MAX_BYTES,
                backupCount=LOG_FILE_BACKUPS,
                encoding="utf-8",
            )
        )
    except OSError as error:
        file_error = error

    logging.basicConfig(format=fmt, level=logging.INFO, handlers=handlers, force=True)
    for name in ("httpx", "httpcore", "aiohttp.access", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if file_error is not None:
        logging.getLogger(__name__).error(
            "file_logging_unavailable error_type=%s",
            type(file_error).__name__,
        )
