"""Буфер сообщений с адаптивной задержкой.

Логика:
- Юзер шлёт сообщение → кладём в Redis-буфер чата.
- Запускаем таймер на base_delay_ms (4-8 сек).
- Если юзер шлёт ещё одно — добавляем к буферу + сбрасываем таймер
  + увеличиваем задержку (+step_delay_ms) до max_delay_ms.
- Когда таймер истёк — флашим: склеиваем все сообщения и отдаём в callback.
- Адаптивная задержка: если бот ответил <30 сек назад — короче (4 сек),
  иначе холодный старт (6 сек).
"""

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable

from cache import get_redis
from config import (
    BUFFER_ENABLED,
    BUFFER_BASE_DELAY_MS,
    BUFFER_ADAPTIVE_BASE_DELAY_MS,
    BUFFER_STEP_DELAY_MS,
    BUFFER_MAX_DELAY_MS,
    BUFFER_ADAPTIVE_MAX_DELAY_MS,
    BUFFER_ADAPTIVE_WINDOW_SEC,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 0.5
LOCK_TTL_MS = 30_000


def _msgs_key(chat_id: int) -> str:
    return f"buf:msgs:{chat_id}"


def _deadline_key(chat_id: int) -> str:
    return f"buf:deadline:{chat_id}"


def _last_resp_key(chat_id: int) -> str:
    return f"buf:last_resp:{chat_id}"


def _lock_key(chat_id: int) -> str:
    return f"buf:lock:{chat_id}"


# Активные чаты, для которых работает poll-таска
_active_chats: set[int] = set()


async def add_message(
    chat_id: int,
    text: str,
    user_id: int | None,
    username: str | None,
    on_flush: Callable[[int, str, int | None, str | None], Awaitable[None]],
) -> None:
    """Добавить сообщение в буфер чата.

    on_flush: коллбэк вызывается когда буфер готов:
        await on_flush(chat_id, combined_text, user_id, username)

    Если BUFFER_ENABLED=false — буферизация выключена, сообщение уходит
    в on_flush немедленно (бот отвечает на каждое сообщение сразу).
    """
    if not BUFFER_ENABLED:
        await on_flush(chat_id, text, user_id, username)
        return

    redis = await get_redis()
    if not redis:
        # Без Redis — отправляем сразу (без буферизации)
        await on_flush(chat_id, text, user_id, username)
        return

    now_ms = int(time.time() * 1000)

    # Адаптивная задержка: если бот недавно отвечал — короче
    last_resp_ms = await redis.get(_last_resp_key(chat_id))
    if last_resp_ms and (now_ms - int(last_resp_ms)) < BUFFER_ADAPTIVE_WINDOW_SEC * 1000:
        base_delay = BUFFER_ADAPTIVE_BASE_DELAY_MS
        max_delay = BUFFER_ADAPTIVE_MAX_DELAY_MS
    else:
        base_delay = BUFFER_BASE_DELAY_MS
        max_delay = BUFFER_MAX_DELAY_MS

    # Кладём сообщение в список
    msg_data = json.dumps(
        {"text": text, "user_id": user_id, "username": username, "ts": now_ms},
        ensure_ascii=False,
    )
    await redis.rpush(_msgs_key(chat_id), msg_data)
    buf_size = await redis.llen(_msgs_key(chat_id))

    # Прогрессивный delay: base + (size-1) * step, capped at max
    delay_ms = min(base_delay + (buf_size - 1) * BUFFER_STEP_DELAY_MS, max_delay)
    deadline_ms = now_ms + delay_ms

    # Обновляем deadline (новое сообщение = сброс таймера)
    await redis.set(_deadline_key(chat_id), deadline_ms, px=delay_ms + 60_000)
    await redis.expire(_msgs_key(chat_id), (delay_ms + 60_000) // 1000)

    # Запускаем poll-таску для чата (если ещё не запущена)
    if chat_id not in _active_chats:
        _active_chats.add(chat_id)
        asyncio.create_task(_poll_chat(chat_id, on_flush))


async def _poll_chat(
    chat_id: int,
    on_flush: Callable[[int, str, int | None, str | None], Awaitable[None]],
) -> None:
    """Polling-цикл для одного чата: ждёт пока таймер истечёт, флашит."""
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SEC)

            redis = await get_redis()
            if not redis:
                break

            deadline_raw = await redis.get(_deadline_key(chat_id))
            if not deadline_raw:
                break

            now_ms = int(time.time() * 1000)
            if int(deadline_raw) > now_ms:
                continue  # таймер ещё активен

            # Дедлайн истёк — пытаемся захватить lock
            acquired = await redis.set(
                _lock_key(chat_id), "1", nx=True, px=LOCK_TTL_MS
            )
            if not acquired:
                continue  # другой воркер уже флашит

            try:
                # Перепроверяем дедлайн под локом (race condition)
                deadline_raw = await redis.get(_deadline_key(chat_id))
                if not deadline_raw or int(deadline_raw) > int(time.time() * 1000):
                    continue

                # Забираем все сообщения и чистим
                raw_msgs = await redis.lrange(_msgs_key(chat_id), 0, -1)
                if not raw_msgs:
                    break

                await redis.delete(_msgs_key(chat_id), _deadline_key(chat_id))

                # Склеиваем
                parsed = [json.loads(m) for m in raw_msgs]
                combined_text = "\n".join(m["text"] for m in parsed)
                last = parsed[-1]
                user_id = last.get("user_id")
                username = last.get("username")

                logger.info(
                    "Буфер чата %s флаш: %d сообщений", chat_id, len(parsed)
                )

                try:
                    await on_flush(chat_id, combined_text, user_id, username)
                except Exception:
                    logger.exception("Ошибка в on_flush для чата %s", chat_id)

                # Метка времени ответа бота
                await redis.set(
                    _last_resp_key(chat_id),
                    int(time.time() * 1000),
                    ex=BUFFER_ADAPTIVE_WINDOW_SEC * 2,
                )
                break

            finally:
                await redis.delete(_lock_key(chat_id))

    finally:
        _active_chats.discard(chat_id)


async def recover_claimed_buffers(
    on_flush: Callable[[int, str, int | None, str | None], Awaitable[None]],
) -> int:
    """Восстановить буферы, оставшиеся в Redis после рестарта/падения бота.

    Сканирует все ключи `buf:msgs:*`, для каждого перезапускает poll-таску.
    poll-таска отдаст сообщения в on_flush, как только истечёт дедлайн
    (если дедлайн уже прошёл — флаш произойдёт сразу).

    Возвращает: количество чатов, для которых восстановили буфер.
    """
    redis = await get_redis()
    if not redis:
        return 0

    recovered = 0
    pattern = _msgs_key(0).replace("0", "*")
    async for key in redis.scan_iter(match=pattern, count=100):
        # Извлекаем chat_id из ключа buf:msgs:{chat_id}
        try:
            chat_id = int(key.rsplit(":", 1)[-1])
        except (ValueError, IndexError):
            continue

        if chat_id in _active_chats:
            continue

        # Если deadline отсутствует — выставляем «срочный» дедлайн на now+200ms,
        # чтобы poll сразу флашнул
        deadline_raw = await redis.get(_deadline_key(chat_id))
        if not deadline_raw:
            now_ms = int(time.time() * 1000)
            await redis.set(_deadline_key(chat_id), now_ms + 200, px=10_000)

        _active_chats.add(chat_id)
        asyncio.create_task(_poll_chat(chat_id, on_flush))
        recovered += 1

    return recovered
