# Апгрейд: ступень 3 → 4 (Буфер)

Добавляет адаптивный буфер сообщений — склейку быстрых сообщений в один LLM-запрос. По умолчанию **выключен**, активируется через локальный скилл `/buffer`.

---

## 1. Новые файлы

```bash
TEMPLATES=~/.codex/skills/llm-project-gradually/templates/step-4-buffer
[ -d "$TEMPLATES" ] || TEMPLATES=<root>/.codex/skills/llm-project-steps/templates/step-4-buffer

cp "$TEMPLATES/project/llm/buffer.py" <root>/project/llm/buffer.py

# Локальный скилл /buffer
mkdir -p <root>/.codex/skills/buffer
cp "$TEMPLATES/.codex/skills/buffer/SKILL.md" <root>/.codex/skills/buffer/SKILL.md
```

---

## 2. Правки в существующих файлах

### `<root>/project/llm/cache.py`

В конец файла добавить функцию `get_redis()` (нужна для `buffer.py`):

```python
async def get_redis() -> aioredis.Redis | None:
    """Доступ к raw Redis-клиенту (для модулей: buffer.py)."""
    if await _ensure_redis():
        return _redis
    return None
```

### `<root>/project/llm/handlers.py`

Это самая большая правка апгрейда. Логика обработки текста переезжает с прямого вызова LLM на буфер.

**А. В импорты добавить:**

```python
from collections.abc import Awaitable, Callable
from buffer import add_message
```

**Б. Под определение `router = Router()` добавить тип:**

```python
OnFlush = Callable[[int, str, int | None, str | None], Awaitable[None]]
```

**В. Заменить ВЕСЬ блок `handle_text` (тело функции). Было:**

```python
@router.message(lambda msg: msg.text and not msg.text.startswith("/"))
async def handle_text(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else None
    username = message.from_user.username if message.from_user else None
    text = message.text or ""

    if await _is_bot_paused():
        await message.answer(BOT_PAUSED_REPLY)
        return

    if len(text) > MAX_INPUT_LENGTH:
        await message.answer(INPUT_TOO_LONG_REPLY.format(limit=MAX_INPUT_LENGTH))
        return

    await db.save_message(chat_id, user_id, "user", text, username)

    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        pass

    context = await cache.get_context(chat_id)
    if not context:
        context = await db.get_context(chat_id)

    if context and context[-1].get("role") == "user" and context[-1].get("content") == text:
        context = context[:-1]

    try:
        result = await generate_response(text, context)
    except Exception:
        logger.exception("LLM упал для chat %s", chat_id)
        await message.answer(
            "Извините, временно не могу ответить. Попробуйте через минуту."
        )
        return

    await cache.push_message(chat_id, "user", text)
    await cache.push_message(chat_id, "assistant", result.text)
    await db.save_message(chat_id, user_id, "assistant", result.text)
    await db.save_token_usage(...)  # если был добавлен на ступени 2

    await message.answer(result.text)
```

**Стало:**

```python
@router.message(lambda msg: msg.text and not msg.text.startswith("/"))
async def handle_text(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else None
    username = message.from_user.username if message.from_user else None
    text = message.text or ""

    if await _is_bot_paused():
        await message.answer(BOT_PAUSED_REPLY)
        return

    if len(text) > MAX_INPUT_LENGTH:
        await message.answer(INPUT_TOO_LONG_REPLY.format(limit=MAX_INPUT_LENGTH))
        return

    # Сохраняем входящее
    await db.save_message(chat_id, user_id, "user", text, username)

    # typing indicator
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        pass

    # В буфер (если выключен — _process_combined вызывается сразу)
    await add_message(chat_id, text, user_id, username, _make_on_flush(bot))


def _make_on_flush(bot: Bot) -> OnFlush:
    """Фабрика callback'а для buffer."""
    async def _cb(chat_id: int, combined_text: str, user_id: int | None, username: str | None) -> None:
        await _process_combined(bot, chat_id, combined_text, user_id, username)
    return _cb


def make_buffer_callback(bot: Bot) -> OnFlush:
    """Публичный API: используется bot.py для recover_claimed_buffers()."""
    return _make_on_flush(bot)


async def _process_combined(
    bot: Bot,
    chat_id: int,
    combined_text: str,
    user_id: int | None,
    username: str | None,
) -> None:
    """Обработать склеенный буфер сообщений: контекст → LLM → ответ."""
    # Повторная проверка тумблера: за время буферизации могли поставить паузу
    if await _is_bot_paused():
        await bot.send_message(chat_id, BOT_PAUSED_REPLY)
        return

    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception:
        pass

    # Контекст из Redis (быстрее) или fallback на БД
    context = await cache.get_context(chat_id)
    if not context:
        context = await db.get_context(chat_id)

    # Уберём из контекста дубль текущего сообщения
    if context and context[-1].get("role") == "user" and context[-1].get("content") == combined_text:
        context = context[:-1]

    try:
        result = await generate_response(combined_text, context)
    except Exception:
        logger.exception("LLM упал для chat %s", chat_id)
        await bot.send_message(
            chat_id,
            "Извините, временно не могу ответить. Попробуйте через минуту.",
        )
        return

    # Сохраняем
    await cache.push_message(chat_id, "user", combined_text)
    await cache.push_message(chat_id, "assistant", result.text)
    await db.save_message(chat_id, user_id, "assistant", result.text)
    await db.save_token_usage(
        chat_id=chat_id, user_id=user_id,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cached_tokens=result.cached_tokens,
        total_tokens=result.total_tokens,
        model=result.model,
    )

    # Отправка
    await bot.send_message(chat_id, result.text)
```

### `<root>/project/llm/bot.py`

**А. В импорт добавить:**

```python
from buffer import recover_claimed_buffers
from handlers import make_buffer_callback, router
```

(`router` уже импортируется — в строку дописать `make_buffer_callback`.)

**Б. В `main()` после `init_llm()` и до `dp.start_polling(...)` добавить:**

```python
    try:
        recovered = await recover_claimed_buffers(make_buffer_callback(bot))
        if recovered:
            logger.info("Recovery: вернули в работу %d буферов", recovered)
    except Exception:
        logger.exception("recover_claimed_buffers упал")
```

### `<root>/project/llm/config.py`

В конец файла добавить:

```python
# --- Буфер сообщений (адаптивная задержка) ---
# По умолчанию ВЫКЛЮЧЕН. Включается через скилл /buffer.
BUFFER_ENABLED = os.getenv("BUFFER_ENABLED", "false").lower() == "true"
BUFFER_BASE_DELAY_MS = int(os.getenv("BUFFER_BASE_DELAY_MS", "6000"))
BUFFER_ADAPTIVE_BASE_DELAY_MS = int(os.getenv("BUFFER_ADAPTIVE_BASE_DELAY_MS", "4000"))
BUFFER_STEP_DELAY_MS = int(os.getenv("BUFFER_STEP_DELAY_MS", "1000"))
BUFFER_MAX_DELAY_MS = int(os.getenv("BUFFER_MAX_DELAY_MS", "8000"))
BUFFER_ADAPTIVE_MAX_DELAY_MS = int(os.getenv("BUFFER_ADAPTIVE_MAX_DELAY_MS", "6000"))
BUFFER_ADAPTIVE_WINDOW_SEC = int(os.getenv("BUFFER_ADAPTIVE_WINDOW_SEC", "30"))
```

---

## 3. .env (доливка)

```
# --- Буфер сообщений (адаптивная задержка) ---
# По умолчанию ВЫКЛЮЧЕН. Включается через скилл /buffer.
BUFFER_ENABLED=false
BUFFER_BASE_DELAY_MS=6000
BUFFER_ADAPTIVE_BASE_DELAY_MS=4000
BUFFER_STEP_DELAY_MS=1000
BUFFER_MAX_DELAY_MS=8000
BUFFER_ADAPTIVE_MAX_DELAY_MS=6000
BUFFER_ADAPTIVE_WINDOW_SEC=30
```

---

## 4. Миграция БД

Не требуется.

---

## 5. Локальные скиллы

`/buffer` — уже скопирован выше (см. п.1). Триггеры: «настроим буфер», «склейка сообщений», «задержка ответа».

---

## 6. Финальное сообщение клиенту

```
Готово. Ступень 4 (Буфер). Добавил:
- buffer.py — адаптивная склейка быстрых сообщений в один LLM-запрос
- recovery после рестарта (буферы переживают перезапуск через Redis)
- По умолчанию ВЫКЛЮЧЕН (BUFFER_ENABLED=false). Бот пока отвечает на каждое сообщение сразу.

Чтобы включить и настроить параметры — скажи "настроим буфер" (запустится локальный скилл /buffer).

Перезапусти: cd project && docker compose up -d --build

Дальше — ступень 5 (Безопасность LLM): защита от jailbreak и утечки промпта.
```
