# Апгрейд: ступень 5 → 6 (Надёжность LLM)

Добавляет резервный LLM-провайдер (fallback), retry с exponential backoff, алерты в Telegram при падениях/недостатке средств.

---

## 1. Новые файлы

```bash
TEMPLATES=~/.codex/skills/llm-project-gradually/templates/step-6-reliability
[ -d "$TEMPLATES" ] || TEMPLATES=<root>/.codex/skills/llm-project-steps/templates/step-6-reliability

cp "$TEMPLATES/project/llm/alerts.py" <root>/project/llm/alerts.py

# Локальный скилл /reliability
mkdir -p <root>/.codex/skills/reliability
cp "$TEMPLATES/.codex/skills/reliability/SKILL.md" <root>/.codex/skills/reliability/SKILL.md
```

---

## 2. Правки в существующих файлах

### `<root>/project/llm/requirements.txt`

Добавить зависимости:
```
anthropic==0.97.0
httpx==0.28.1
```

### `<root>/project/llm/llm.py`

**Эта правка большая — целиком меняется логика вызова LLM.** Ученик должен заменить файл на новую версию или применить инструкции по блокам.

**А. В импорты добавить:**

```python
import asyncio
import json
import time
import redis.asyncio as aioredis

from config import (
    LLM_MAX_RETRIES,
    REDIS_URL,
    RESERVE_API_KEY,
    RESERVE_BASE_URL,
    RESERVE_MODEL,
)
from alerts import send_admin_alert
```

**Б. Добавить глобальные переменные резервного клиента:**

```python
_reserve_client = None
_reserve_kind: str = ""
```

**В. В `init_llm()` после создания основного клиента добавить создание резервного:**

```python
    # Резервный провайдер (опциональный)
    if RESERVE_API_KEY and RESERVE_MODEL:
        _reserve_kind = _detect_kind(RESERVE_MODEL, RESERVE_BASE_URL)
        _reserve_client = _create_client(RESERVE_API_KEY, RESERVE_BASE_URL, _reserve_kind)
        logger.info(
            "Резервный LLM-клиент создан: kind=%s, model=%s, base_url=%s",
            _reserve_kind, RESERVE_MODEL, RESERVE_BASE_URL or "(default)",
        )
    else:
        logger.info("Резервная LLM не настроена (RESERVE_API_KEY/RESERVE_MODEL пусты)")
```

И в начало `init_llm()` объявить глобалы — заменить `global _primary_client, _primary_kind` на:

```python
    global _primary_client, _primary_kind, _reserve_client, _reserve_kind
```

**Г. Добавить функции `_set_funds_status` и `_is_insufficient_funds`:**

```python
FUNDS_STATUS_KEY = "llm:funds:{provider}"


async def _set_funds_status(
    provider: str,
    status: str,
    model: str,
    detail: str = "",
) -> None:
    """Записать статус провайдера в Redis. provider: 'main' | 'reserve'."""
    try:
        client = aioredis.from_url(REDIS_URL, decode_responses=True)
        payload = json.dumps({
            "status": status,
            "model": model,
            "ts": int(time.time()),
            "detail": detail[:200],
        }, ensure_ascii=False)
        await client.set(FUNDS_STATUS_KEY.format(provider=provider), payload)
        await client.aclose()
    except Exception:
        logger.exception("Не смог записать статус провайдера %s в Redis", provider)


def _is_insufficient_funds(error: Exception) -> bool:
    """Проверить, связана ли ошибка с недостатком средств."""
    if hasattr(error, "status_code") and error.status_code == 402:
        return True
    error_str = str(error).lower()
    markers = ("insufficient", "quota exceeded", "payment required", "billing", "credit")
    if hasattr(error, "status_code") and error.status_code == 429:
        return any(m in error_str for m in markers)
    return "insufficient_funds" in error_str or "payment required" in error_str
```

**Д. Заменить функцию `_invoke` на принимающую клиент параметром:**

Было:
```python
async def _invoke(messages: list[dict]) -> object:
    if _primary_kind == "anthropic":
        ...
        response = await _primary_client.messages.create(...)
        ...
    return await _primary_client.chat.completions.create(...)
```

Стало:
```python
async def _invoke(client, kind: str, model: str, messages: list[dict]) -> object:
    """Универсальный вызов LLM. Возвращает ответ в OpenAI-совместимом формате."""
    if kind == "anthropic":
        system, msgs = _convert_messages_for_anthropic(messages)
        response = await client.messages.create(
            model=model,
            max_tokens=LLM_MAX_TOKENS,
            system=system,
            messages=msgs,
            temperature=LLM_TEMPERATURE,
        )
        return _anthropic_to_openai_format(response)

    return await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=LLM_TEMPERATURE,
        max_tokens=LLM_MAX_TOKENS,
    )
```

**Е. Добавить каскадную функцию `_llm_call`:**

```python
async def _llm_call(messages: list[dict]) -> object:
    """Каскадный вызов LLM: основной → ретраи → резервный → ретраи → исключение."""
    last_error: Exception | None = None

    # Фаза 1: основной провайдер
    for attempt in range(1 + LLM_MAX_RETRIES):
        try:
            response = await _invoke(_primary_client, _primary_kind, LLM_MODEL, messages)
            await _set_funds_status("main", "ok", LLM_MODEL)
            return response
        except Exception as e:
            last_error = e
            if _is_insufficient_funds(e):
                await _set_funds_status("main", "depleted", LLM_MODEL, str(e))
                await send_admin_alert(
                    error_type="LLM — недостаток средств (основной)",
                    details=str(e),
                    severity="CRITICAL",
                )
                break
            if attempt < LLM_MAX_RETRIES:
                wait = 2 ** attempt
                logger.warning(
                    "Основной LLM ошибка (попытка %d/%d), повтор через %d сек: %s",
                    attempt + 1, 1 + LLM_MAX_RETRIES, wait, e,
                )
                await asyncio.sleep(wait)

    logger.error("Основной LLM недоступен после %d попыток", 1 + LLM_MAX_RETRIES)

    # Фаза 2: резервный провайдер (если настроен)
    if _reserve_client:
        await send_admin_alert(
            error_type="LLM — переключение на резервный",
            details=str(last_error),
            severity="WARNING",
        )
        for attempt in range(1 + LLM_MAX_RETRIES):
            try:
                response = await _invoke(_reserve_client, _reserve_kind, RESERVE_MODEL, messages)
                await _set_funds_status("reserve", "ok", RESERVE_MODEL)
                return response
            except Exception as e:
                last_error = e
                if _is_insufficient_funds(e):
                    await _set_funds_status("reserve", "depleted", RESERVE_MODEL, str(e))
                    await send_admin_alert(
                        error_type="LLM — недостаток средств (резервный)",
                        details=str(e),
                        severity="CRITICAL",
                    )
                    break
                if attempt < LLM_MAX_RETRIES:
                    wait = 2 ** attempt
                    logger.warning(
                        "Резервный LLM ошибка (попытка %d/%d), повтор через %d сек: %s",
                        attempt + 1, 1 + LLM_MAX_RETRIES, wait, e,
                    )
                    await asyncio.sleep(wait)

    await send_admin_alert(
        error_type="LLM полностью недоступен",
        details=f"Все провайдеры упали: {last_error}",
        severity="CRITICAL",
    )
    raise last_error
```

**Ж. В `generate_response()` заменить `response = await _invoke(messages)` на `response = await _llm_call(messages)`.**

### `<root>/project/llm/bot.py`

**А. В импорты добавить:**

```python
from alerts import send_admin_alert, close_alerts
```

**Б. В `main()` обернуть init вызовов в try/except:**

Было:
```python
    await cache.init_cache()
    await db.init_db()
    init_llm()
```

Стало:
```python
    try:
        await cache.init_cache()
    except Exception as e:
        logger.exception("Не удалось подключиться к Redis")
        await send_admin_alert(
            error_type="Redis — ошибка при старте",
            details=str(e),
            severity="CRITICAL",
        )

    try:
        await db.init_db()
    except Exception as e:
        logger.exception("Не удалось подключиться к PostgreSQL")
        await send_admin_alert(
            error_type="PostgreSQL — ошибка при старте",
            details=str(e),
            severity="CRITICAL",
        )

    init_llm()
```

**В. В `_global_error_handler` для `TelegramBadRequest` добавить алерт:**

Было:
```python
    if isinstance(exc, TelegramBadRequest):
        logger.error("Telegram BadRequest: %s", exc)
        return True
```

Стало:
```python
    if isinstance(exc, TelegramBadRequest):
        logger.error("Telegram BadRequest: %s", exc)
        await send_admin_alert(
            error_type="Telegram BadRequest",
            details=f"{exc}\nUpdate id: {getattr(event.update, 'update_id', '?')}",
            severity="WARNING",
        )
        return True
```

И в catch необработанных ошибок:
```python
    logger.exception("Необработанная ошибка в aiogram", exc_info=exc)
    await send_admin_alert(
        error_type="Bot — необработанное исключение",
        details=f"{type(exc).__name__}: {exc}",
        severity="CRITICAL",
    )
    return True
```

**Г. В `main()` finally перед `await bot.session.close()` добавить:**

```python
        await close_alerts()
```

### `<root>/project/llm/handlers.py`

**А. В импорты добавить:**

```python
from alerts import send_admin_alert
```

**Б. В `_process_combined()` в catch блоке `generate_response` добавить алерт:**

Было:
```python
    try:
        result = await generate_response(combined_text, context)
    except Exception:
        logger.exception("LLM упал для chat %s", chat_id)
        await bot.send_message(...)
        return
```

Стало:
```python
    try:
        result = await generate_response(combined_text, context)
    except Exception as e:
        logger.exception("LLM упал для chat %s", chat_id)
        await send_admin_alert(
            error_type="LLM — необработанное исключение",
            details=str(e),
            severity="CRITICAL",
            chat_id=chat_id, user_id=user_id, username=username,
        )
        await bot.send_message(
            chat_id,
            "Извините, временно не могу ответить. Попробуйте через минуту.",
        )
        return
```

**В. В блоках `check_input` / `check_output` (если ступень 5 уже применена) добавить `send_admin_alert(severity="WARNING")` после `save_security_incident`.**

**Г. Обернуть `_process_combined` в верхнеуровневый try/except и алертить:**

```python
async def _process_combined(...) -> None:
    try:
        # вся логика
        ...
    except Exception as e:
        logger.exception("Необработанное исключение в _process_combined")
        await send_admin_alert(
            error_type="Bot — необработанное исключение",
            details=str(e),
            severity="CRITICAL",
            chat_id=chat_id, user_id=user_id, username=username,
        )
```

### `<root>/project/llm/cache.py`

**А. В импорты добавить:**

```python
from alerts import send_admin_alert
```

**Б. В `_ensure_redis()` при reconnect добавить алерт WARNING, при полном падении — CRITICAL:**

Заменить блок:
```python
    try:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        await _redis.ping()
        logger.info("Redis: соединение восстановлено")
        return True
    except Exception:
        logger.exception("Redis недоступен")
        _redis = None
        return False
```

На:
```python
    try:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
        await _redis.ping()
        await send_admin_alert(
            error_type="Redis — переподключение",
            details="Соединение восстановлено",
            severity="WARNING",
        )
        return True
    except Exception as e:
        await send_admin_alert(
            error_type="Redis недоступен",
            details=str(e),
            severity="CRITICAL",
        )
        _redis = None
        return False
```

### `<root>/project/llm/db.py`

**А. В импорты добавить:**

```python
from alerts import send_admin_alert
```

**Б. В `_ensure_pool()` при ошибке reconnect — алерт CRITICAL:**

Заменить:
```python
    except Exception:
        logger.exception("PostgreSQL недоступен")
        _pool = None
        return False
```

На:
```python
    except Exception as e:
        await send_admin_alert(
            error_type="PostgreSQL недоступен", details=str(e), severity="CRITICAL"
        )
        _pool = None
        return False
```

### `<root>/project/llm/config.py`

В конец добавить:

```python
# --- LLM (резервный провайдер, опциональный) ---
RESERVE_API_KEY = os.getenv("RESERVE_API_KEY", "")
RESERVE_BASE_URL = os.getenv("RESERVE_BASE_URL", "") or None
RESERVE_MODEL = os.getenv("RESERVE_MODEL", "")

# --- LLM настройки (надёжность) ---
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))

# --- Алерты администратору ---
ADMIN_TG_CHAT_ID = os.getenv("ADMIN_TG_CHAT_ID", "")
ALERT_RATE_LIMIT_SEC = int(os.getenv("ALERT_RATE_LIMIT_SEC", "300"))
BALANCE_CHECK_INTERVAL_SEC = int(os.getenv("BALANCE_CHECK_INTERVAL_SEC", "21600"))
```

### Восстановление неотвеченных сообщений после рестарта

Чтобы бот догонял сообщения, на которые не успел ответить из-за рестарта/падения
(сообщение юзера сохранено в БД, но ответ не ушёл).

**`db.py`** — после `get_context()` добавить две функции:

```python
async def mark_chat_answered(chat_id: int) -> None:
    """Пометить user-сообщения чата отвеченными — после успешной отправки ответа."""
    if not await _ensure_pool():
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                "UPDATE messages SET answered = TRUE "
                "WHERE chat_id = $1 AND role = 'user' AND answered = FALSE",
                chat_id,
            )
    except Exception:
        logger.exception("Ошибка mark_chat_answered")


async def get_unanswered(window_min: int) -> list[dict]:
    """User-сообщения без ответа за последние window_min минут."""
    if not await _ensure_pool():
        return []
    try:
        async with _pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT ON (chat_id) chat_id, user_id, username, content "
                "FROM messages "
                "WHERE role = 'user' AND answered = FALSE "
                "AND created_at > NOW() - make_interval(mins => $1) "
                "ORDER BY chat_id, created_at DESC",
                window_min,
            )
        return [
            {"chat_id": r["chat_id"], "user_id": r["user_id"],
             "username": r["username"], "content": r["content"]}
            for r in rows
        ]
    except Exception:
        logger.exception("Ошибка get_unanswered")
        return []
```

**`handlers.py`** — в `_process_combined()` после `await bot.send_message(chat_id, result.text)`:

```python
        # Помечаем сообщения чата отвеченными (для recovery после рестарта)
        await db.mark_chat_answered(chat_id)
```

**`bot.py`** — добавить функцию `_recover_unanswered` (перед `main()`) и задачу
в `background_tasks`:

```python
async def _recover_unanswered(bot: Bot) -> None:
    """Догнать user-сообщения, на которые бот не успел ответить (рестарт/краш)."""
    try:
        pending = await db.get_unanswered(RECOVER_UNANSWERED_WINDOW_MIN)
    except Exception:
        logger.exception("get_unanswered упал")
        return
    if not pending:
        return
    callback = make_buffer_callback(bot)
    recovered = 0
    for item in pending:
        chat_id = item["chat_id"]
        # Чат уже подхвачен из буфера (recover_claimed_buffers) — не дублируем
        if await has_pending_buffer(chat_id):
            continue
        try:
            await callback(
                chat_id, item["content"],
                item["user_id"], item["username"],
            )
            recovered += 1
        except Exception:
            logger.exception("Ошибка догона чата %s", chat_id)
    if recovered:
        logger.info("Recovery: догнал %d неотвеченных сообщений", recovered)
```

В `background_tasks` добавить:
```python
        asyncio.create_task(_recover_unanswered(bot), name="recover_unanswered"),
```
В импорт из `config` — `RECOVER_UNANSWERED_WINDOW_MIN`, из `buffer` —
`has_pending_buffer` (определяется в блоке про graceful shutdown ниже).

**`config.py`** — добавить:

```python
# --- Восстановление после рестарта ---
RECOVER_UNANSWERED_WINDOW_MIN = int(os.getenv("RECOVER_UNANSWERED_WINDOW_MIN", "30"))
```

### Graceful shutdown ждёт обработку сообщений

Чтобы при `docker compose restart/down` бот дожидался не только фоновых задач,
но и активной обработки буферов сообщений (иначе ответ обрывается на полуслове).

**`buffer.py`** — после `_active_chats` добавить реестр poll-задач:

```python
# Активные poll-задачи — для ожидания при graceful shutdown
_poll_tasks: set[asyncio.Task] = set()


def _spawn_poll(
    chat_id: int,
    on_flush: Callable[[int, str, int | None, str | None], Awaitable[None]],
) -> None:
    """Запустить poll-таску чата и зарегистрировать её для graceful shutdown."""
    _active_chats.add(chat_id)
    task = asyncio.create_task(_poll_chat(chat_id, on_flush))
    _poll_tasks.add(task)
    task.add_done_callback(_poll_tasks.discard)


def inflight_tasks() -> set[asyncio.Task]:
    """Незавершённые poll-задачи — bot.py дожидается их при остановке."""
    return {t for t in _poll_tasks if not t.done()}


async def has_pending_buffer(chat_id: int) -> bool:
    """Есть ли несфлашенный буфер чата в Redis (чтобы recovery не дублировал)."""
    redis = await get_redis()
    if not redis:
        return False
    try:
        return bool(await redis.exists(_msgs_key(chat_id)))
    except Exception:
        return False
```

И заменить в `add_message` и `recover_claimed_buffers` пары строк
`_active_chats.add(chat_id)` + `asyncio.create_task(_poll_chat(...))` на один
вызов `_spawn_poll(chat_id, on_flush)` — он сам добавляет чат в `_active_chats`.

**`bot.py`** — в импорт `from buffer import ...` добавить `inflight_tasks`.
В блоке `finally`, где собирается `inflight`, добавить активные буфер-задачи:

```python
        inflight = [t for t in background_tasks if not t.done()]
        inflight += list(inflight_tasks())
```

### Rate-limit на пользователя (антифлуд)

Защита от флуда сообщениями (флуд = лишние запросы к LLM = счёт за токены).

**`cache.py`** — добавить функцию (и в импорт из `config` — `RATE_LIMIT_MESSAGES`,
`RATE_LIMIT_WINDOW_SEC`):

```python
async def check_rate_limit(user_id: int) -> bool:
    """True — юзер в пределах лимита, False — превысил."""
    if not await _ensure_redis():
        return True
    try:
        key = f"ratelimit:{user_id}"
        async with _redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, RATE_LIMIT_WINDOW_SEC)
            count, _ = await pipe.execute()
        return count <= RATE_LIMIT_MESSAGES
    except Exception:
        logger.exception("Ошибка check_rate_limit")
        return True
```

**`handlers.py`** — в `handle_text()` после проверки тумблера (и в импорт из
`config` — `RATE_LIMIT_REPLY`):

```python
    # Rate-limit: защита от флуда сообщениями
    if user_id is not None and not await cache.check_rate_limit(user_id):
        logger.warning("Rate-limit для user %s (chat %s)", user_id, chat_id)
        await message.answer(RATE_LIMIT_REPLY)
        return
```

**`config.py`** — добавить:

```python
# --- Rate-limit (антифлуд) ---
RATE_LIMIT_MESSAGES = int(os.getenv("RATE_LIMIT_MESSAGES", "20"))
RATE_LIMIT_WINDOW_SEC = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_REPLY = os.getenv(
    "RATE_LIMIT_REPLY",
    "Слишком много сообщений подряд. Подождите минуту, пожалуйста.",
)
```

---

## 3. .env (доливка)

```
# --- LLM (резервный провайдер, опциональный) ---
RESERVE_API_KEY=
RESERVE_MODEL=
RESERVE_BASE_URL=

# --- LLM настройки (надёжность) ---
LLM_MAX_RETRIES=1

# --- Алерты администратору ---
ADMIN_TG_CHAT_ID=
ALERT_RATE_LIMIT_SEC=300

# --- Восстановление после рестарта ---
RECOVER_UNANSWERED_WINDOW_MIN=30

# --- Rate-limit (антифлуд) ---
RATE_LIMIT_MESSAGES=20
RATE_LIMIT_WINDOW_SEC=60
```

---

## 4. Миграция БД

Колонка `messages.answered` (для recovery неотвеченных сообщений) добавляется
идемпотентно через `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` в `init_db()` —
на работающей прод-БД применится автоматически при старте. Отдельных шагов не
требуется.

---

## 5. Локальные скиллы

`/reliability` — уже скопирован выше (см. п.1).

Триггеры: «настроим резерв», «настроим fallback», «надёжность LLM», «куда слать алерты».

---

## 6. Финальное сообщение клиенту

```
Готово. Ступень 6 (Надёжность LLM). Добавил:
- alerts.py — алерты в Telegram (CRITICAL / WARNING) с rate-limit
- Каскадный retry с exponential backoff (LLM_MAX_RETRIES)
- Резервный LLM-провайдер: когда основной падает — переключается на резервный
- Детектор недостатка средств: при 402/insufficient → CRITICAL-алерт, не ретраит
- Алерты при падениях Redis / Postgres / aiogram

По умолчанию резервная модель НЕ настроена. Чтобы настроить — скажи "настроим резерв" (запустится локальный скилл /reliability).

Перезапусти (с пересборкой — добавились зависимости): cd project && docker compose up -d --build

Дальше — ступень 7 (Деплой): сервер, SSH, бэкапы Postgres.
```
