# Апгрейд: ступень 2 → 3 (Эвалы)

Добавляет систему эвалюэйшенов: тест-кейсы, прогоны через LLM-судью с regex+LLM-as-judge каскадом, прогресс через SSE.

---

## 1. Новые файлы (просто скопировать)

```bash
TEMPLATES=~/.codex/skills/llm-project-gradually/templates/step-3-evals
[ -d "$TEMPLATES" ] || TEMPLATES=<root>/.codex/skills/llm-project-steps/templates/step-3-evals

# eval-runner и датасеты в LLM-контейнере
cp -R "$TEMPLATES/project/llm/eval" <root>/project/llm/eval

# eval-роуты и шаблоны в админке
cp "$TEMPLATES/project/admin/eval_database.py" <root>/project/admin/eval_database.py
cp "$TEMPLATES/project/admin/eval_routes.py"   <root>/project/admin/eval_routes.py
cp "$TEMPLATES/project/admin/eval_runner.py"   <root>/project/admin/eval_runner.py
cp "$TEMPLATES/project/admin/templates/"eval*.html <root>/project/admin/templates/

# Локальный скилл /evals
mkdir -p <root>/.codex/skills/evals
cp "$TEMPLATES/.codex/skills/evals/SKILL.md" <root>/.codex/skills/evals/SKILL.md
```

---

## 2. Правки в существующих файлах

### `<root>/project/llm/db.py`

В функции `init_db()` после блока `prompt_versions` добавить таблицы эвалов:

```python
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_cases (
                id BIGSERIAL PRIMARY KEY,
                category VARCHAR(64) NOT NULL DEFAULT 'general',
                question TEXT NOT NULL,
                expected_keywords TEXT[] NOT NULL DEFAULT '{}',
                forbidden_keywords TEXT[] NOT NULL DEFAULT '{}',
                expected_answer TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_runs (
                id BIGSERIAL PRIMARY KEY,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                total INTEGER NOT NULL DEFAULT 0,
                passed INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(16) NOT NULL DEFAULT 'running',
                judge_model VARCHAR(64),
                error_message TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_results (
                id BIGSERIAL PRIMARY KEY,
                run_id BIGINT NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
                case_id BIGINT REFERENCES eval_cases(id) ON DELETE SET NULL,
                question TEXT NOT NULL,
                expected_answer TEXT NOT NULL,
                actual_answer TEXT,
                verdict VARCHAR(32) NOT NULL,
                check_layer VARCHAR(16),
                score REAL,
                judge_reasoning TEXT,
                duration_ms INTEGER,
                error_message TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_eval_results_run
            ON eval_results (run_id, id)
        """)
```

### `<root>/project/admin/app.py`

**А. В импорты добавить eval-роутер:**

```python
from eval_routes import router as eval_router  # noqa: E402
```

(Вставить рядом с другими `from ..._routes import router`.)

**Б. После `app.include_router(prompt_router)` добавить:**

```python
app.include_router(eval_router)
```

### `<root>/project/admin/templates/base.html`

В блоке `<div class="nav-links">` добавить пункт меню «Эвалы» между «Промпт» и «Управление»:

```html
            <a href="/prompt/">Промпт</a>
            <a href="/eval/">Эвалы</a>
            <a href="/bot-control/">Управление</a>
```

### `<root>/project/llm/config.py`

В конец файла добавить:

```python
# --- Eval (LLM-as-judge) ---
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-4.1-mini")
JUDGE_API_KEY = os.getenv("JUDGE_API_KEY", "") or LLM_API_KEY
JUDGE_BASE_URL = os.getenv("JUDGE_BASE_URL", "") or LLM_BASE_URL
JUDGE_PASS_THRESHOLD = float(os.getenv("JUDGE_PASS_THRESHOLD", "0.8"))
```

---

## 3. .env (доливка)

```
# --- Eval (LLM-as-judge) ---
# Модель которая судит ответы бота при прогоне эвалов.
JUDGE_MODEL=gpt-4.1-mini
# Если пусто — используется LLM_API_KEY.
JUDGE_API_KEY=
# Если пусто — используется LLM_BASE_URL.
JUDGE_BASE_URL=
# Порог score для verdict=pass (0.0-1.0). Дефолт 0.8 (строго).
JUDGE_PASS_THRESHOLD=0.8
```

---

## 4. Миграция БД

```bash
cd <root>/project && docker compose exec llm python -c "import asyncio, db; asyncio.run(db.init_db())"
```

(Можно пропустить — таблицы создадутся автоматически при следующем рестарте `llm`.)

---

## 5. Локальные скиллы

`evals/` — уже скопирован выше (см. п.1).

Триггеры скилла: «настроим эвалы», «делаем эвалюэйшены», «добавим тест-кейс».

---

## 6. Финальное сообщение клиенту

```
Готово. Ступень 3 (Эвалы).
- В админке появился раздел /eval/ — CRUD тест-кейсов и прогоны
- Двухступенчатая проверка: regex/keywords → LLM-as-judge при необходимости
- Прогресс прогона через Server-Sent Events (живой прогресс-бар)
- Adversarial-датасет (jailbreak-тесты) — отдельно через CLI:
  docker compose exec llm python -m eval.run_evals --only adversarial

Локальный скилл /evals — настройка judge-модели и наполнение dataset.json
(вручную или из реальной истории чатов).

Перезапусти: cd project && docker compose up -d --build

Дальше — ступень 4 (Буфер): склейка быстрых сообщений в один LLM-запрос.
```
