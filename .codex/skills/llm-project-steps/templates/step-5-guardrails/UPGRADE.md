# Апгрейд: ступень 4 → 5 (Безопасность LLM / Guardrails + PII-маскирование)

Добавляет два слоя безопасности:
1. **Guardrails** — защита от jailbreak и утечки промпта: 43 regex по 7 категориям атак на вход, детектор утечки на выход, sanitize_input, anti-injection преамбула.
2. **PII-маскирование** — соответствие 152-ФЗ: regex+валидаторы (СНИЛС, ИНН, паспорт, ОМС, карта, телефон, email, IP, дата рождения, свидетельство о рождении) + опциональный NER (Presidio + spaCy ru_core_news_lg для имён/локаций/организаций).

---

## 1. Новые файлы

```bash
TEMPLATES=~/.codex/skills/llm-project-gradually/templates/step-5-guardrails
[ -d "$TEMPLATES" ] || TEMPLATES=<root>/.codex/skills/llm-project-steps/templates/step-5-guardrails

cp "$TEMPLATES/project/llm/guardrails.py" <root>/project/llm/guardrails.py
cp "$TEMPLATES/project/llm/prompts/_guardrails_preamble.md" <root>/project/llm/prompts/_guardrails_preamble.md
cp "$TEMPLATES/project/llm/pii.py" <root>/project/llm/pii.py

# Локальный скилл /guardrails
mkdir -p <root>/.codex/skills/guardrails
cp "$TEMPLATES/.codex/skills/guardrails/SKILL.md" <root>/.codex/skills/guardrails/SKILL.md
```

---

## 2. Правки в существующих файлах

### `<root>/project/llm/handlers.py`

**А. В импорты добавить:**

```python
from guardrails import (
    check_input,
    check_output,
    GUARDRAIL_REFUSAL,
    GUARDRAIL_OUTPUT_FALLBACK,
)
```

**Б. В `handle_text()` после проверки `MAX_INPUT_LENGTH` и до `await db.save_message(...)` добавить:**

```python
    # Слой 1: input guardrail
    ok, reason = check_input(text)
    if not ok:
        logger.warning("Input заблокирован для chat %s: %s", chat_id, reason)
        await db.save_security_incident(
            chat_id=chat_id, user_id=user_id, username=username,
            incident_type="input_blocked",
            user_message=text, blocked_response=None, reason=reason or "",
        )
        await message.answer(GUARDRAIL_REFUSAL)
        return
```

**В. В `_process_combined()` после получения `result` от `generate_response` и до `cache.push_message` добавить:**

```python
    # Слой 3: output guardrail
    ok, reason = check_output(result.text)
    if not ok:
        logger.warning("Output заблокирован для chat %s: %s", chat_id, reason)
        await db.save_security_incident(
            chat_id=chat_id, user_id=user_id, username=username,
            incident_type="output_blocked",
            user_message=combined_text, blocked_response=result.text,
            reason=reason or "",
        )
        await bot.send_message(chat_id, GUARDRAIL_OUTPUT_FALLBACK)
        return
```

### `<root>/project/llm/llm.py`

**А. В импорты добавить:**

```python
from config import GUARDRAILS_PREAMBLE_PATH
from guardrails import sanitize_input
```

**Б. Добавить глобальную:**

```python
_guardrails_preamble: str = ""
```

(Рядом с `_system_prompt: str = ""`.)

**В. Расширить `_load_prompt` чтобы читал и преамбулу. Заменить:**

```python
def _load_prompt() -> None:
    global _system_prompt
    if SYSTEM_PROMPT_PATH.exists():
        _system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    else:
        _system_prompt = ""
```

**На:**

```python
def _load_prompt() -> None:
    global _system_prompt, _guardrails_preamble
    if GUARDRAILS_PREAMBLE_PATH.exists():
        _guardrails_preamble = GUARDRAILS_PREAMBLE_PATH.read_text(encoding="utf-8").strip()
    else:
        _guardrails_preamble = ""
    if SYSTEM_PROMPT_PATH.exists():
        _system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()
    else:
        _system_prompt = ""


def _build_system_message() -> str:
    """Финальный system-промпт = guardrails preamble + клиентский system.md."""
    parts = [p for p in (_guardrails_preamble, _system_prompt) if p]
    return "\n\n".join(parts)
```

**Г. В `generate_response` заменить блок:**

```python
    messages: list[dict] = []
    if _system_prompt:
        messages.append({"role": "system", "content": _system_prompt})
```

**На:**

```python
    user_message = sanitize_input(user_message)

    messages: list[dict] = []
    system_msg = _build_system_message()
    if system_msg:
        messages.append({"role": "system", "content": system_msg})
```

### `<root>/project/llm/db.py`

**А. В `init_db()` после блока `eval_results` (или `prompt_versions` если эвалов ещё нет) добавить таблицу security_incidents:**

```python
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS security_incidents (
                id BIGSERIAL PRIMARY KEY,
                chat_id BIGINT NOT NULL,
                user_id BIGINT,
                username VARCHAR(255),
                incident_type VARCHAR(32) NOT NULL,
                user_message TEXT,
                blocked_response TEXT,
                reason TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_security_chat_created
            ON security_incidents (chat_id, created_at DESC)
        """)
```

**Б. Добавить функцию `save_security_incident`:**

```python
async def save_security_incident(
    chat_id: int,
    user_id: int | None,
    username: str | None,
    incident_type: str,
    user_message: str | None,
    blocked_response: str | None,
    reason: str,
) -> None:
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO security_incidents
                   (chat_id, user_id, username, incident_type,
                    user_message, blocked_response, reason)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                chat_id, user_id, username, incident_type,
                user_message, blocked_response, reason,
            )
    except Exception:
        logger.exception("Ошибка сохранения security_incident")
```

**В. В `cleanup_old_records()` добавить `security_incidents` в список tables:**

```python
tables = ("messages", "token_usage", "security_incidents")
```

### `<root>/project/llm/config.py`

В конец добавить:

```python
# --- Guardrails ---
GUARDRAILS_PREAMBLE_PATH = Path(__file__).resolve().parent / "prompts" / "_guardrails_preamble.md"


def _env_bool(key: str, default: bool = False) -> bool:
    """Распарсить bool из env: 'true'/'1'/'yes'/'on' → True."""
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


# По умолчанию ВЫКЛЮЧЕНЫ. Включаются через скилл /guardrails.
# sanitize_input и preamble работают всегда.
GUARDRAILS_INPUT_ENABLED = _env_bool("GUARDRAILS_INPUT_ENABLED", False)
GUARDRAILS_OUTPUT_ENABLED = _env_bool("GUARDRAILS_OUTPUT_ENABLED", False)
_VALID_GUARDRAIL_CATEGORIES = frozenset({
    "role_switch", "prompt_leak", "authority", "system_tags",
    "policy_patch", "separator", "known_attack",
})
GUARDRAILS_INPUT_CATEGORIES: frozenset[str] = frozenset(
    c.strip() for c in os.getenv("GUARDRAILS_INPUT_CATEGORIES", "").split(",")
    if c.strip() and c.strip() in _VALID_GUARDRAIL_CATEGORIES
)

# --- PII-маскирование (152-ФЗ: трансграничная передача) ---
# По умолчанию ВЫКЛЮЧЕНО. Включается через .env когда LLM зарубежный
# (OpenAI/Anthropic/OpenRouter), а сервер в РФ.
# PII_MASK_ENABLED=true — слой 1: regex+валидаторы (паспорт/СНИЛС/ИНН/карта/...).
# PII_NER_ENABLED=true  — слой 2: имена/локации/организации через Presidio + spaCy.
#                         Требует пересборки образа с INSTALL_SPACY_RU=true (см. Dockerfile).
PII_MASK_ENABLED = _env_bool("PII_MASK_ENABLED", False)
PII_NER_ENABLED = _env_bool("PII_NER_ENABLED", False)
```

### `<root>/project/llm/requirements.txt`

В конец добавить (закомментировано — раскомментировать при `PII_NER_ENABLED=true`):

```
# --- PII NER (опционально, ~200 МБ + 560 МБ модель в Docker-образе) ---
# Раскомментируй если PII_NER_ENABLED=true в .env, иначе оставь как есть.
# presidio-analyzer==2.2.354
# presidio-anonymizer==2.2.354
# spacy==3.7.5
```

### `<root>/project/llm/Dockerfile`

После строки `RUN pip install --no-cache-dir -r requirements.txt` добавить:

```dockerfile
# Опционально: загрузка spaCy-модели для PII NER (русский язык).
# Включается через build-arg INSTALL_SPACY_RU=true (передаётся из docker-compose.yml).
ARG INSTALL_SPACY_RU=false
RUN if [ "$INSTALL_SPACY_RU" = "true" ]; then \
        python -m spacy download ru_core_news_lg; \
    fi
```

### `<root>/project/docker-compose.yml`

В сервис `bot` секцию `build:` добавить `args:` (если ещё нет):

```yaml
  bot:
    build:
      context: .
      dockerfile: llm/Dockerfile
      args:
        INSTALL_SPACY_RU: ${PII_NER_ENABLED:-false}
```

(Compose читает `PII_NER_ENABLED` из `.env` и пробрасывает в build-arg. Образ пересобирается с моделью только когда тумблер `true`.)

### `<root>/project/llm/handlers.py` (PII)

**А. К импортам guardrails добавить импорт PII:**

```python
from pii import mask_pii, restore_pii
```

**Б. В `_process_combined()` после получения `combined_text` (текст, который пойдёт в LLM) и ДО вызова `generate_response(...)` добавить:**

```python
    # PII masking — 152-ФЗ: маскируем перед отправкой в зарубежный LLM
    combined_text_masked, pii_mapping = mask_pii(combined_text)
```

Везде ниже в `_process_combined()` передавать в `generate_response` уже **`combined_text_masked`** вместо `combined_text`. Также при `cache.push_message(role="user", content=...)` для текущего сообщения сохранять **`combined_text_masked`** — чтобы Redis-контекст для будущих запросов не содержал ПДн.

В Postgres (`db.save_message(...)`) оставить **оригинальный `combined_text`** — для админки, статистики и истории.

**В. После guardrails check_output (где уже есть `result.text`) добавить восстановление:**

```python
    # Восстанавливаем PII в ответе LLM (placeholder → оригинал)
    reply_text = restore_pii(result.text, pii_mapping)
```

Везде ниже отправку клиенту, сохранение в `db.save_message(role="assistant", ...)` и `cache.push_message(role="assistant", ...)` делать с **`reply_text`** вместо `result.text`.

---

## 3. .env (доливка)

```
# --- Guardrails (защита от jailbreak и утечки промпта) ---
# По умолчанию ВЫКЛЮЧЕНЫ. sanitize_input и preamble работают всегда.
GUARDRAILS_INPUT_ENABLED=false
GUARDRAILS_OUTPUT_ENABLED=false
GUARDRAILS_INPUT_CATEGORIES=

# --- PII-маскирование (152-ФЗ: трансграничная передача данных) ---
# По умолчанию ВЫКЛЮЧЕНО. Включай если LLM зарубежный (OpenAI/Anthropic/OpenRouter),
# а сервер в РФ — данные пользователей не должны уходить за рубеж в открытом виде.
#
# PII_MASK_ENABLED=true  — Слой 1 (всегда полезен): regex+валидаторы.
#                          Маскирует: паспорт РФ, СНИЛС, ИНН (10/12), банк. карту (Луна),
#                          ОМС (Mod10), телефон РФ, email, IP, дату рождения,
#                          свидетельство о рождении.
#                          Доп. зависимостей нет.
#
# PII_NER_ENABLED=true   — Слой 2 (опц.): имена, локации, организации через
#                          Presidio + spaCy ru_core_news_lg. Требует:
#                          1) Раскомментировать presidio/spacy в requirements.txt.
#                          2) Пересобрать образ: `docker compose build bot`
#                             (Compose сам пробросит INSTALL_SPACY_RU=true).
#                          Образ распухнет на ~600 МБ, +200 мс на сообщение.
PII_MASK_ENABLED=false
PII_NER_ENABLED=false
```

---

## 4. Миграция БД

```bash
cd <root>/project && docker compose exec bot python -c "import asyncio, db; asyncio.run(db.init_db())"
```

---

## 5. Локальные скиллы

`/guardrails` — уже скопирован выше (см. п.1). Триггеры: «настроим guardrails», «защита от jailbreak».

---

## 6. Auto-canary напоминание

Если `<root>/project/llm/prompts/system.md` непустой — после апгрейда сообщи клиенту дополнительно:

```
У тебя уже есть системный промпт. Чтобы добавить canary-токены защиты от утечки промпта — перезапусти скилл /llm-setup и выбери раздел «Промпт». Он прочитает свежий system.md, выдернет уникальные фразы и впишет их в _OUTPUT_LEAK_PATTERNS в guardrails.py.
```

Если `system.md` пустой — пропустить напоминание (нечего защищать).

---

## 7. Финальное сообщение клиенту

```
Готово. Ступень 5 (Безопасность LLM). Добавил два слоя защиты:

GUARDRAILS (защита от jailbreak и утечки промпта):
- guardrails.py — 43 regex по 7 категориям атак (role_switch, prompt_leak, authority, ...)
- _guardrails_preamble.md — anti-injection преамбула (всегда подключается к промпту)
- sanitize_input — нормализация unicode, удаление zero-width символов и HTML-тегов (всегда)
- security_incidents в БД — что блокировал guardrails

По умолчанию regex-проверки ВЫКЛЮЧЕНЫ. sanitize_input и preamble работают всегда.
Включить regex-защиты — через локальный скилл /guardrails.

PII-МАСКИРОВАНИЕ (152-ФЗ — трансграничная передача):
- pii.py — маскирует ПДн в сообщении ПЕРЕД отправкой в зарубежный LLM,
  восстанавливает в ответе. В Postgres — оригинал, в Redis — маскированный текст.
- Слой 1 (regex+валидаторы): паспорт РФ, СНИЛС, ИНН, банк. карта (Луна), ОМС,
  телефон, email, IP, дата рождения, свидетельство о рождении.
- Слой 2 (опц., spaCy NER): имена, локации, организации в свободном тексте.

По умолчанию ВЫКЛЮЧЕНО. Включай если LLM зарубежный (OpenAI/Anthropic),
а сервер в РФ. См. .env: PII_MASK_ENABLED, PII_NER_ENABLED.

Перезапусти: cd project && docker compose up -d --build

Дальше — ступень 6 (Надёжность LLM): резервный провайдер, retry, алерты в Telegram.
```
