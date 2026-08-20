"""Disabled legacy polling entrypoint.

Telegram updates are accepted only by ``webhook:app`` so consent, buffering,
the durable inbox/outbox pipeline and idempotency cannot be bypassed.
"""

import asyncio
import logging
from logging.handlers import RotatingFileHandler

from logging_config import configure_logging


def _setup_logging() -> None:
    configure_logging(RotatingFileHandler)


_setup_logging()
logger = logging.getLogger(__name__)


async def main() -> None:
    raise RuntimeError("Telegram polling is disabled; use webhook runtime")


if __name__ == "__main__":
    asyncio.run(main())
