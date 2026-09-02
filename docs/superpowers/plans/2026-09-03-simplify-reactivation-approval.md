# Simplify Reactivation Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить обязательный `legal reference` и ввод `АКТИВИРОВАТЬ` одним понятным owner-only запуском, не ослабляя consent и pre-send проверки.

**Architecture:** Существующий `activate_version` получает флаг `start_program`, чтобы активация версии и перевод программы в `active` выполнялись в одной PostgreSQL-транзакции. `legal_*` остаются в schema и старом owner-only endpoint для rollback, но удаляются из activation/runtime gates и UI. Никаких новых таблиц, миграций, зависимостей или JavaScript-файлов.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, Jinja2, pytest, Docker Compose.

## Global Constraints

- Точный текст: «Сейчас подходят N клиентов. Сообщение будет отправлено только тем, кто дал согласие на рассылку».
- Точный диалог: «Запустить рассылку? Сообщение получат только клиенты, которые согласились на рассылку».
- Owner-only, CSRF, свежий preview, test-send, data freshness, identity и все per-recipient fences сохраняются.
- При `eligible = 0` запуск блокируется.
- `legal_*` остаются deprecated без migration и не участвуют в текущем запуске или доставке.
- Production, staging rollout и клиентские сообщения не входят в локальную реализацию.

---

### Task 1: Удалить `legal_*` из реальных gates

**Files:**
- Modify: `project/tests/integration/reactivation/test_preview.py`
- Modify: `project/tests/integration/reactivation/test_delivery_fence.py`
- Modify: `project/tests/integration/reactivation/test_journey_planner.py`
- Modify: `project/src/moroz/reactivation/repository.py`

**Interfaces:**
- Consumes: `ReactivationRepository.activate_version`, `_check_activation_gates`, `_runtime_gates_open`.
- Produces: запуск и runtime delivery, не зависящие от deprecated `legal_*`; остальные gates неизменны.

- [x] **Step 1: Write failing activation and runtime tests**

Добавить проверку, которая перед запуском явно оставляет `legal_status='pending'` и `legal_reference=NULL`, но ожидает успешную активацию после свежего preview/test. Из существующих delivery/planner тестов убрать ожидание остановки только из-за `legal_status='pending'` и вместо него доказать, что consent и остальные fences продолжают решать допуск.

```python
async def test_activation_does_not_require_deprecated_legal_fields(repository, database):
    value, version_id, owner_id = repository
    await value.preview_version(version_id, actor_id=owner_id, now=NOW)
    async with database.acquire() as connection:
        await connection.execute(
            "UPDATE reactivation_settings SET legal_status='pending', "
            "legal_reference=NULL, legal_approved_at=NULL, legal_approved_by=NULL "
            "WHERE id=1"
        )
    activated = await value.activate_version(version_id, owner_id, NOW)
    assert activated["status"] == "active"
```

- [x] **Step 2: Verify RED in Docker**

Run:

```bash
docker compose --env-file ../.env run --rm test pytest -q tests/integration/reactivation/test_preview.py tests/integration/reactivation/test_delivery_fence.py tests/integration/reactivation/test_journey_planner.py
```

Expected: новый activation test падает с `ActivationBlocked("legal_approved")`; старые runtime expectations показывают зависимость от legal-полей.

- [x] **Step 3: Implement the minimum gate change**

Удалить только четыре `legal_*` условия из `_check_activation_gates`, delivery SQL `program_active` и `_runtime_gates_open`. Не менять marketing consent, suppression, freshness, identity и recipient-lock логику.

```python
def _runtime_gates_open(settings) -> bool:
    return bool(
        settings
        and settings["mode"] == "active"
        and settings["active_version_id"] is not None
    )
```

- [x] **Step 4: Verify GREEN in Docker**

Повторить команду Step 2. Expected: весь focused integration набор проходит.

- [x] **Step 5: Commit**

```bash
git add project/src/moroz/reactivation/repository.py project/tests/integration/reactivation
git commit -m "refactor: убран legal reference из gate реактивации"
```

### Task 2: Сделать запуск одним атомарным действием

**Files:**
- Modify: `project/src/moroz/reactivation/repository.py`
- Modify: `project/admin/reactivation_database.py`
- Modify: `project/admin/reactivation_routes.py`
- Modify: `project/tests/integration/reactivation/test_preview.py`
- Modify: `project/tests/e2e/admin/test_marketing_reactivation.py`

**Interfaces:**
- Consumes: `activate_version(version_id, actor_id, now)` и POST `/marketing/versions/{version_id}/activate`.
- Produces: `activate_version(version_id, actor_id, now, *, start_program: bool = False)`; admin wrapper передаёт `start_program=True`.

- [x] **Step 1: Write failing atomic-launch tests**

Integration test вызывает `activate_version(version_id, owner_id, NOW, start_program=True)` и проверяет одной выборкой, что version status и settings mode стали `active`, а audit содержит владельца и `preview_eligible`. Admin E2E отправляет только CSRF, без `confirmation`, и проверяет один вызов wrapper с `start_program=True`.

```python
launched = await value.activate_version(
    version_id, owner_id, NOW, start_program=True
)
assert launched["status"] == "active"
assert await connection.fetchval(
    "SELECT mode FROM reactivation_settings WHERE id=1"
) == "active"
```

Добавить RED на `eligible=0` с ожидаемым `ActivationBlocked("eligible_recipients")`.

- [x] **Step 2: Verify RED in Docker**

Run:

```bash
docker compose --env-file ../.env run --rm test pytest -q tests/integration/reactivation/test_preview.py tests/e2e/admin/test_marketing_reactivation.py
```

Expected: `start_program` ещё не поддерживается, route всё ещё требует `АКТИВИРОВАТЬ`, нулевая аудитория не блокируется.

- [x] **Step 3: Implement atomic launch**

Добавить keyword-only `start_program=False`. При `True` в той же транзакции записать `mode='active'`, `stopped_at=NULL`, active version и increment `program_revision`. В `reactivation.version_activated` добавить безопасные audit-поля `mode` и `preview_eligible`, без текста сообщения и recipient IDs. В wrapper admin передать `start_program=True`; из routes удалить проверку слова `АКТИВИРОВАТЬ`. Для resume оставить owner-only + CSRF + те же repository gates, но убрать текстовую фразу.

```python
async def activate_version(
    self, version_id: UUID, actor_id: int, now: datetime, *, start_program: bool = False
) -> dict:
    current = _aware(now)

preview_eligible = int((version["preview_counts"] or {}).get("eligible", 0))
if start_program and preview_eligible == 0:
    raise ActivationBlocked("eligible_recipients")
start_mode = "active" if start_program else settings["mode"]
```

- [x] **Step 4: Verify GREEN in Docker**

Повторить команду Step 2. Expected: focused integration/admin E2E проходит.

- [x] **Step 5: Commit**

```bash
git add project/src/moroz/reactivation/repository.py project/admin/reactivation_database.py project/admin/reactivation_routes.py project/tests
git commit -m "feat: запуск реактивации одним действием"
```

### Task 3: Упростить интерфейс и синхронизировать документацию

**Files:**
- Modify: `project/admin/templates/reactivation.html`
- Modify: `project/tests/unit/admin/test_reactivation_routes.py`
- Modify: `project/tests/e2e/admin/test_marketing_reactivation.py`
- Modify: `ТЗ и архитектура.md`
- Modify: `Дорожная карта.md`
- Modify: `changelog.md`

**Interfaces:**
- Consumes: `draft.preview_counts.eligible`, existing activate/mode POST routes.
- Produces: четыре шага readiness и точный согласованный русский текст запуска.

- [x] **Step 1: Write failing template contract**

```python
assert "Сейчас подходят" in html
assert "Сообщение будет отправлено только тем, кто дал согласие на рассылку." in html
assert "Запустить рассылку? Сообщение получат только клиенты, которые согласились на рассылку." in html
for removed in ("Юридическое подтверждение", "Ссылка или номер документа", "АКТИВИРОВАТЬ"):
    assert removed not in html
```

- [x] **Step 2: Verify RED in Docker**

Run:

```bash
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_reactivation_routes.py tests/e2e/admin/test_marketing_reactivation.py
```

Expected: старый template содержит legal form/`АКТИВИРОВАТЬ` и не содержит новый текст.

- [x] **Step 3: Implement the approved copy**

Удалить legal readiness/action, перенумеровать запуск в шаг 4, показать динамический `eligible`, кнопку `Запустить` и `confirm('Запустить рассылку? Сообщение получат только клиенты, которые согласились на рассылку.')`. Для нуля показать причину и disabled button. Notice после успешного POST: «Рассылка запущена.»

- [x] **Step 4: Update owner documents**

В `ТЗ и архитектура.md` заменить legal reference/typed confirmation на owner launch с visible count и audit; в дорожной карте отметить локальную часть после фактических проверок; сразу записывать каждый завершённый шаг в `changelog.md`.

- [x] **Step 5: Run full verification**

```bash
docker compose --env-file ../.env run --rm test pytest -q tests/unit/admin/test_reactivation_routes.py tests/e2e/admin/test_marketing_reactivation.py tests/integration/reactivation tests/e2e/reactivation/test_reactivation_v2.py
docker compose --env-file ../.env run --rm test python -m compileall -q admin src
docker compose --env-file ../.env config --quiet
git diff --check
```

Expected: все команды exit `0`; старые consent/runtime fences остаются зелёными.

- [x] **Step 6: Browser QA**

Поднять только Docker local admin stack, проверить desktop `1440 px` и mobile `390 px`: точный текст, `N`, отсутствие legal form/typed phrase, disabled zero-state, confirm dialog и отсутствие horizontal overflow. Не отправлять реальные Telegram-сообщения.

- [x] **Step 7: Commit**

```bash
git add project/admin/templates/reactivation.html project/tests ТЗ\ и\ архитектура.md Дорожная\ карта.md changelog.md
git commit -m "feat: упрощён интерфейс запуска реактивации"
```

После локальной проверки ветка готова к review/merge. Push и staging rollout выполняются только по отдельному явному разрешению владельца.
