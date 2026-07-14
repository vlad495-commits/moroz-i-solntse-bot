# Апгрейд: ступень 1 → 2 (Админка)

Добавляет FastAPI-админку: список диалогов, статистика, токены, редактор промпта с rollback, тумблер «Бот вкл/выкл», логи.

---

## 1. Новые файлы (просто скопировать)

```bash
TEMPLATES=~/.codex/skills/llm-project-gradually/templates/step-2-admin
# Если глобального скилла нет — взять из локальной копии:
[ -d "$TEMPLATES" ] || TEMPLATES=<root>/.codex/skills/llm-project-steps/templates/step-2-admin

cp -R "$TEMPLATES/project/admin" <root>/project/admin
```

**Подмена плейсхолдеров в скопированных файлах админки:**

В `admin/app.py` и `admin/templates/base.html` есть `{{PROJECT_NAME}}` — его надо заменить на имя проекта (то которое вводилось при создании на ступени 1, обычно совпадает с заголовком в `<root>/AGENTS.md`).

```bash
# Извлекаем имя проекта из первой строки AGENTS.md (формат: "# {{PROJECT_NAME}}")
PROJECT_NAME=$(awk 'NR==1 && /^# / {print substr($0, 3); exit}' <root>/AGENTS.md)

# Подменяем во всех файлах админки где встречается {{PROJECT_NAME}}
grep -rl "{{PROJECT_NAME}}" <root>/project/admin/ | while read -r f; do
    sed -i '' "s/{{PROJECT_NAME}}/${PROJECT_NAME}/g" "$f" 2>/dev/null || \
    sed -i "s/{{PROJECT_NAME}}/${PROJECT_NAME}/g" "$f"
done
```

После копирования:
- `<root>/project/admin/` — содержит ~10 .py + Dockerfile + .dockerignore + requirements.txt + templates/ (9 файлов) + static/styles.css.
- Все `{{PROJECT_NAME}}` заменены на имя из `AGENTS.md` (например, «Innokentiy Bot»).

---

## 2. Правки в существующих файлах

### `<root>/project/llm/db.py`

**А. В функции `init_db()` после блока CREATE TABLE messages добавить:**

```python
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS token_usage (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                model VARCHAR(64) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_token_usage_chat_created
            ON token_usage (chat_id, created_at DESC)
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                id BIGSERIAL PRIMARY KEY,
                content TEXT NOT NULL,
                author VARCHAR(64),
                comment TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prompt_versions_created
            ON prompt_versions (created_at DESC)
        """)
```

**Б. Добавить функцию `save_token_usage` (после `get_context`):**

```python
async def save_token_usage(
    chat_id: int,
    user_id: int | None,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    total_tokens: int,
    model: str,
) -> None:
    if not await _ensure_pool():
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO token_usage
                   (chat_id, user_id, prompt_tokens, completion_tokens,
                    cached_tokens, total_tokens, model)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                chat_id, user_id, prompt_tokens, completion_tokens,
                cached_tokens, total_tokens, model,
            )
    except Exception:
        logger.exception("Ошибка сохранения token_usage")
```

**В. В `cleanup_old_records()` добавить таблицу `token_usage` в кортеж tables:**

Заменить:
```python
result = {}
try:
    async with _pool.acquire() as conn:
        status = await conn.execute(
            "DELETE FROM messages "
            "WHERE created_at < NOW() - ($1 || ' days')::INTERVAL",
            str(DATA_RETENTION_DAYS),
        )
        result["messages"] = int(status.split()[-1])
    return result
```

На:
```python
tables = ("messages", "token_usage")
result = {}
try:
    async with _pool.acquire() as conn:
        for table in tables:
            status = await conn.execute(
                f"DELETE FROM {table} "
                f"WHERE created_at < NOW() - ($1 || ' days')::INTERVAL",
                str(DATA_RETENTION_DAYS),
            )
            result[table] = int(status.split()[-1])
    return result
```

### `<root>/project/llm/handlers.py`

**А. В импорты добавить:**

```python
from config import (
    BOT_PAUSE_KEY,
    BOT_PAUSED_REPLY,
    INPUT_TOO_LONG_REPLY,
    MAX_INPUT_LENGTH,
    NON_TEXT_REPLY,
    START_REPLY,
)
```

**Б. После строки `router = Router()` добавить функцию:**

```python
async def _is_bot_paused() -> bool:
    """Глобальный тумблер `Бот вкл/выкл` — флаг bot:paused в Redis."""
    redis = await cache.get_redis() if hasattr(cache, "get_redis") else None
    if not redis:
        # На ступени 1 cache.get_redis() нет — берём напрямую через push_message нет смысла,
        # вернём False (без админки тумблер не управляется).
        return False
    try:
        return bool(await redis.get(BOT_PAUSE_KEY))
    except Exception:
        return False
```

(Если `get_redis` отсутствует в cache.py — функция вернёт False, тумблер просто не сработает. На ступени 4 при добавлении буфера `get_redis` появится и тумблер заработает корректно.)

**В. В `handle_start()` в самом начале добавить:**

```python
if await _is_bot_paused():
    await message.answer(BOT_PAUSED_REPLY)
    return
```

**Г. В `handle_text()` после `text = message.text or ""` добавить:**

```python
if await _is_bot_paused():
    await message.answer(BOT_PAUSED_REPLY)
    return
```

**Д. В `handle_non_text()` в самом начале добавить:**

```python
if await _is_bot_paused():
    await message.answer(BOT_PAUSED_REPLY)
    return
```

**Е. После `await message.answer(result.text)` (в конце `handle_text`) добавить сохранение токенов:**

```python
await db.save_token_usage(
    chat_id=chat_id, user_id=user_id,
    prompt_tokens=result.prompt_tokens,
    completion_tokens=result.completion_tokens,
    cached_tokens=result.cached_tokens,
    total_tokens=result.total_tokens,
    model=result.model,
)
```

### `<root>/project/llm/llm.py`

**А. В импорты добавить:**

```python
import asyncio
import redis.asyncio as aioredis

from config import REDIS_URL, PROMPT_RELOAD_CHANNEL
```

**Б. В конце файла добавить `prompt_reload_listener`:**

```python
async def prompt_reload_listener() -> None:
    """Слушает Redis pub/sub `prompt:reload`. Перечитывает промпт без рестарта.

    Админка публикует в этот канал после сохранения новой версии промпта.
    """
    backoff = 1.0
    while True:
        try:
            client = aioredis.from_url(REDIS_URL, decode_responses=True)
            pubsub = client.pubsub()
            await pubsub.subscribe(PROMPT_RELOAD_CHANNEL)
            logger.info("Подписка на канал %s активна", PROMPT_RELOAD_CHANNEL)
            backoff = 1.0
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                _load_prompt()
                logger.info(
                    "Промпт перечитан с диска: %d символов (триггер: %s)",
                    len(_system_prompt), msg.get("data"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Сбой prompt_reload_listener, повтор через %.1f сек", backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
```

### `<root>/project/llm/bot.py`

**А. В импорт из `llm` добавить `prompt_reload_listener`:**

```python
from llm import init_llm, prompt_reload_listener
```

**Б. В `main()` в `background_tasks` добавить задачу:**

Заменить:
```python
background_tasks: list[asyncio.Task] = [
    asyncio.create_task(_cleanup_loop(), name="cleanup_loop"),
]
```

На:
```python
background_tasks: list[asyncio.Task] = [
    asyncio.create_task(_cleanup_loop(), name="cleanup_loop"),
    asyncio.create_task(prompt_reload_listener(), name="prompt_reload_listener"),
]
```

### `<root>/project/llm/config.py`

В конец файла добавить:

```python
# --- Админка ---
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "change-me-in-production")
ADMIN_SESSION_TTL_SEC = int(os.getenv("ADMIN_SESSION_TTL_SEC", "86400"))

# --- Hot-reload промпта ---
PROMPT_RELOAD_CHANNEL = "prompt:reload"

# --- Тумблер бота ---
BOT_PAUSE_KEY = "bot:paused"
BOT_PAUSED_REPLY = os.getenv(
    "BOT_PAUSED_REPLY",
    "Сейчас бот на технической паузе. Мы скоро вернёмся.",
)

# --- Логи в админке ---
LOGS_TAIL_LINES = int(os.getenv("LOGS_TAIL_LINES", "300"))

# --- Pricing (для расчёта стоимости в админке) ---
PRICING_PER_1M_TOKENS = {
    "gpt-4.1-mini": {"prompt": 0.40, "completion": 1.60, "cache_discount": 0.75},
    "gpt-4.1": {"prompt": 2.00, "completion": 8.00, "cache_discount": 0.75},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60, "cache_discount": 0.50},
    "gpt-4o": {"prompt": 2.50, "completion": 10.00, "cache_discount": 0.50},
    "claude-haiku-4-5": {"prompt": 1.00, "completion": 5.00, "cache_discount": 0.90},
    "claude-sonnet-4-6": {"prompt": 3.00, "completion": 15.00, "cache_discount": 0.90},
}
```

### `<root>/project/docker-compose.yml`

После сервиса `bot:` добавить сервис `admin:` (вставить блок перед `redis:`):

```yaml
  admin:
    build: ./admin
    restart: unless-stopped
    env_file: ../.env
    stop_grace_period: 30s
    volumes:
      - ./llm/prompts:/app/prompts:rw
      - ./logs:/app/logs:ro
    ports:
      - "127.0.0.1:8080:8080"
    depends_on:
      postgres:
        condition: service_healthy
      redis:
        condition: service_healthy
    logging:
      driver: "json-file"
      options:
        max-size: "5m"
        max-file: "3"
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/login', timeout=3).status == 200 else 1)\""]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
```

---

## 3. .env (доливка — добавить в конец, существующие НЕ трогать)

```
# --- Админка (FastAPI) ---
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
ADMIN_SESSION_SECRET=ЗАМЕНИТЬ_НА_openssl_rand_hex_32
ADMIN_SESSION_TTL_SEC=86400

# --- Логи в админке ---
LOGS_TAIL_LINES=300
```

`ADMIN_SESSION_SECRET` сгенерировать через `openssl rand -hex 32` и подставить вместо плейсхолдера.

`BOT_PAUSED_REPLY` уже мог быть в .env (если ученик настраивал через `/speaking-style` на ступени 1) — НЕ переписываем. Если его нет — тоже ничего не делаем, дефолт подхватится из `config.py`.

---

## 4. Миграция БД (ОБЯЗАТЕЛЬНЫЙ ШАГ)

Создаёт таблицы `messages`, `token_usage`, `prompt_versions`. Без них админка падает с
`UndefinedTableError` (дашборд читает `messages`, статистика — `token_usage`).

```bash
cd <root>/project && docker compose run --rm --no-deps bot python -c "import asyncio, db; asyncio.run(db.init_db())"
```

**Почему `run`, а не `exec`:** контейнер `bot` не стартует без валидного `TELEGRAM_BOT_TOKEN`
(проверка токена в `bot.py` стоит раньше `db.init_db()`). Если у клиента токена ещё нет —
`exec bot` невозможен, и таблицы не создаются. `docker compose run --rm --no-deps bot`
поднимает одноразовый контейнер из того же образа, выполняет миграцию и удаляется —
работает независимо от того, запущен ли основной `llm`.

После выполнения проверь, что таблицы на месте:
```bash
docker compose exec postgres psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "\dt"
```

Таблицу `security_incidents` ступень 2 НЕ создаёт — она появляется на ступени 5.
Админка ступени 2 к ней обращается мягко (через `to_regclass`), без падения.

---

## 5. Локальные скиллы

Ничего нового добавлять не нужно. На этой ступени активируется уже существующий `BOT_PAUSED_REPLY` (настраивается через `/speaking-style`).

---

## 6. Финальное сообщение клиенту

```
Готово. Ступень 2 (Админка). Добавил:
- FastAPI-админка на http://localhost:8080 (admin/admin — поменяй перед продом через /production-ready)
- Разделы: Диалоги, Статистика, Промпт (редактор + версии + rollback), Управление (тумблер бот вкл/выкл), Логи
- Тумблер «Бот вкл/выкл» в админке управляет ответами бота через Redis-флаг
- Hot-reload промпта: меняешь в админке → бот перечитывает без рестарта
- Метрики токенов и стоимости запросов в БД

Перезапусти стек: cd project && docker compose up -d --build

Дальше — ступень 3 (Эвалы): тест-кейсы, прогоны через LLM-судью.
```
